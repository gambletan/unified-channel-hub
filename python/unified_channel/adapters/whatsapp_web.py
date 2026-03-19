"""WhatsApp Web adapter — connects to whatsapp-web.js bridge via HTTP.

Unofficial adapter using whatsapp-web.js (no Meta Business API needed).
Scan QR code with your personal WhatsApp to authenticate.

Setup:
  1. cd bridges/whatsapp-web && npm install && node index.js
  2. Scan QR at http://localhost:8084/qr
  3. Configure this adapter with bridge_url="http://localhost:8084"

Later, switch to official WhatsApp Business API by changing the adapter
in config — no other code changes needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator

import httpx

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


class WhatsAppWebAdapter(ChannelAdapter):
    """
    Unofficial WhatsApp adapter via whatsapp-web.js bridge.

    Connects to a local Node.js bridge that wraps whatsapp-web.js.
    Messages are received via SSE (Server-Sent Events) and sent via REST.
    """

    channel_id = "whatsapp"

    def __init__(
        self,
        bridge_url: str = "http://localhost:8084",
        *,
        command_prefix: str = "/",
    ) -> None:
        self._bridge_url = bridge_url.rstrip("/")
        self._prefix = command_prefix
        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._authenticated = False
        self._last_activity: datetime | None = None
        self._http: httpx.AsyncClient | None = None
        self._sse_task: asyncio.Task | None = None
        self._phone: str = ""

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(base_url=self._bridge_url, timeout=30)

        # Check bridge is running
        try:
            resp = await self._http.get("/status")
            data = resp.json()
            self._authenticated = data.get("authenticated", False)
            self._phone = data.get("phone", "")
            if self._authenticated:
                logger.info("whatsapp-web connected: %s", self._phone)
            else:
                logger.info("whatsapp-web bridge reachable, waiting for QR scan at %s/qr", self._bridge_url)
        except Exception as e:
            logger.warning("whatsapp-web bridge not reachable at %s: %s", self._bridge_url, e)

        self._connected = True

        # Start SSE listener for incoming messages
        self._sse_task = asyncio.create_task(self._listen_sse())

    async def disconnect(self) -> None:
        self._connected = False
        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
        if self._http:
            await self._http.aclose()
        logger.info("whatsapp-web disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await self._queue.get()
                yield msg
            except asyncio.CancelledError:
                break

    async def send(self, msg: OutboundMessage) -> str | None:
        if not self._http:
            raise RuntimeError("whatsapp-web not connected")

        try:
            resp = await self._http.post("/send", json={
                "to": msg.chat_id,
                "text": msg.text,
            })
            if resp.status_code == 200:
                data = resp.json()
                self._last_activity = datetime.now()
                return data.get("id")
            else:
                logger.error("whatsapp-web send failed: %d %s", resp.status_code, resp.text)
                return None
        except Exception as e:
            logger.error("whatsapp-web send error: %s", e)
            return None

    async def get_status(self) -> ChannelStatus:
        # Refresh auth status
        if self._http:
            try:
                resp = await self._http.get("/status")
                data = resp.json()
                self._authenticated = data.get("authenticated", False)
                self._phone = data.get("phone", "")
            except Exception:
                pass

        return ChannelStatus(
            connected=self._connected and self._authenticated,
            channel="whatsapp",
            account_id=self._phone or "pending-qr",
            last_activity=self._last_activity,
        )

    async def _listen_sse(self) -> None:
        """Listen to the bridge's SSE /messages endpoint for incoming messages."""
        while self._connected:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", f"{self._bridge_url}/messages") as resp:
                        async for line in resp.aiter_lines():
                            if not self._connected:
                                break
                            if not line.startswith("data: "):
                                continue
                            try:
                                data = json.loads(line[6:])
                                unified = self._parse_message(data)
                                if unified:
                                    await self._queue.put(unified)
                                    self._last_activity = datetime.now()
                                    self._authenticated = True
                            except json.JSONDecodeError:
                                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._connected:
                    logger.warning("whatsapp-web SSE connection lost: %s, reconnecting in 5s", e)
                    await asyncio.sleep(5)

    def _parse_message(self, data: dict) -> UnifiedMessage | None:
        """Parse bridge message event into UnifiedMessage."""
        text = data.get("text", "")
        msg_type = data.get("type", "chat")
        from_number = data.get("from", "")
        sender_name = data.get("sender_name", "")

        if msg_type == "chat" and text:
            if text.startswith(self._prefix):
                parts = text[len(self._prefix):].split()
                cmd = parts[0] if parts else ""
                args = parts[1:]
                content = MessageContent(type=ContentType.COMMAND, text=text, command=cmd, args=args)
            else:
                content = MessageContent(type=ContentType.TEXT, text=text)
        elif msg_type in ("image", "video", "audio", "document", "sticker"):
            content = MessageContent(
                type=ContentType.MEDIA,
                text=text,
                media_type=msg_type,
            )
        else:
            return None

        try:
            ts = datetime.fromtimestamp(int(data.get("timestamp", 0)))
        except (ValueError, OSError):
            ts = datetime.now()

        return UnifiedMessage(
            id=data.get("id", ""),
            channel="whatsapp",
            sender=Identity(id=from_number, display_name=sender_name or from_number),
            content=content,
            timestamp=ts,
            chat_id=from_number,
            raw=data,
        )
