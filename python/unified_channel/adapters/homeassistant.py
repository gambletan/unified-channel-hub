"""Home Assistant adapter — control smart home devices and receive state changes.

Requires: pip install httpx websockets

Uses Home Assistant REST API + WebSocket API for real-time state updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator

from ..adapter import ChannelAdapter
from ..types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)

logger = logging.getLogger(__name__)


class HomeAssistantAdapter(ChannelAdapter):
    """Home Assistant adapter — smart home as a messaging channel."""

    channel_id = "homeassistant"

    def __init__(
        self,
        url: str,
        access_token: str,
        *,
        entity_filters: list[str] | None = None,
        ws_path: str = "/api/websocket",
    ) -> None:
        """
        Args:
            url: Home Assistant base URL (e.g., http://homeassistant.local:8123)
            access_token: Long-lived access token
            entity_filters: Optional list of entity_id prefixes to subscribe to
                           (e.g., ["light.", "switch.", "sensor.temperature"])
        """
        self._url = url.rstrip("/")
        self._token = access_token
        self._entity_filters = entity_filters
        self._ws_url = self._url.replace("http", "ws") + ws_path

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._ws_task: asyncio.Task | None = None
        self._msg_id = 0

    async def connect(self) -> None:
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._connected = True
        logger.info("homeassistant connected: %s", self._url)

    async def disconnect(self) -> None:
        self._connected = False
        if self._ws_task:
            self._ws_task.cancel()
        logger.info("homeassistant disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await self._queue.get()
                yield msg
            except asyncio.CancelledError:
                break

    async def send(self, msg: OutboundMessage) -> str | None:
        """Call a Home Assistant service.

        Expected msg fields:
        - chat_id: entity_id (e.g., "light.living_room")
        - text: service to call (e.g., "turn_on", "turn_off", "toggle")
        - metadata: optional service data dict
        """
        import httpx

        entity_id = msg.chat_id
        service = msg.text
        domain = entity_id.split(".")[0] if "." in entity_id else entity_id

        service_data = {"entity_id": entity_id}
        if msg.metadata:
            service_data.update(msg.metadata)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._url}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {self._token}"},
                json=service_data,
            )
            resp.raise_for_status()

        self._last_activity = datetime.now()
        return entity_id

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="homeassistant",
            account_id=self._url,
            last_activity=self._last_activity,
        )

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _should_include(self, entity_id: str) -> bool:
        if not self._entity_filters:
            return True
        return any(entity_id.startswith(f) for f in self._entity_filters)

    async def _ws_loop(self) -> None:
        import websockets

        while self._connected:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    # Auth phase
                    auth_msg = await ws.recv()
                    auth_data = json.loads(auth_msg)
                    if auth_data.get("type") == "auth_required":
                        await ws.send(json.dumps({
                            "type": "auth",
                            "access_token": self._token,
                        }))
                        result = json.loads(await ws.recv())
                        if result.get("type") != "auth_ok":
                            logger.error("homeassistant auth failed: %s", result)
                            return

                    # Subscribe to state changes
                    sub_id = self._next_id()
                    await ws.send(json.dumps({
                        "id": sub_id,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    }))

                    logger.info("homeassistant ws subscribed to state_changed")

                    async for raw in ws:
                        data = json.loads(raw)
                        if data.get("type") != "event":
                            continue
                        event = data.get("event", {})
                        event_data = event.get("data", {})
                        entity_id = event_data.get("entity_id", "")

                        if not self._should_include(entity_id):
                            continue

                        new_state = event_data.get("new_state", {})
                        old_state = event_data.get("old_state", {})
                        state_val = new_state.get("state", "")
                        old_val = old_state.get("state", "") if old_state else ""

                        if state_val == old_val:
                            continue

                        friendly = new_state.get("attributes", {}).get(
                            "friendly_name", entity_id
                        )
                        text = f"{friendly}: {old_val} → {state_val}"
                        self._last_activity = datetime.now()

                        msg = UnifiedMessage(
                            id=f"{entity_id}:{data.get('id', '')}",
                            channel="homeassistant",
                            sender=Identity(
                                id=entity_id,
                                username=entity_id,
                                display_name=friendly,
                            ),
                            content=MessageContent(type=ContentType.TEXT, text=text),
                            chat_id=entity_id,
                            raw=event_data,
                        )
                        await self._queue.put(msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("homeassistant ws error: %s, reconnecting...", e)
                await asyncio.sleep(5)
