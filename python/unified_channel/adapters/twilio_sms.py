"""Twilio SMS adapter — send and receive SMS/MMS via Twilio.

Requires: pip install twilio aiohttp

Inbound: Twilio webhook → local aiohttp server → UnifiedMessage
Outbound: REST API to send SMS
"""

from __future__ import annotations

import asyncio
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


class TwilioSMSAdapter(ChannelAdapter):
    """Twilio SMS/MMS adapter."""

    channel_id = "twilio_sms"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        phone_number: str,
        *,
        webhook_host: str = "0.0.0.0",
        webhook_port: int = 8081,
        webhook_path: str = "/sms",
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._phone_number = phone_number
        self._webhook_host = webhook_host
        self._webhook_port = webhook_port
        self._webhook_path = webhook_path

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._server_task: asyncio.Task | None = None
        self._client = None

    async def connect(self) -> None:
        from twilio.rest import Client

        self._client = Client(self._account_sid, self._auth_token)
        self._server_task = asyncio.create_task(self._run_webhook_server())
        self._connected = True
        logger.info("twilio sms connected: %s on :%d%s",
                     self._phone_number, self._webhook_port, self._webhook_path)

    async def disconnect(self) -> None:
        self._connected = False
        if self._server_task:
            self._server_task.cancel()
        logger.info("twilio sms disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, msg: OutboundMessage) -> str | None:
        if not self._client:
            raise RuntimeError("twilio sms not connected")

        loop = asyncio.get_running_loop()
        kwargs = {"to": msg.chat_id, "from_": self._phone_number, "body": msg.text}
        if msg.metadata and msg.metadata.get("media_url"):
            kwargs["media_url"] = [msg.metadata["media_url"]]

        sms = await loop.run_in_executor(
            None, lambda: self._client.messages.create(**kwargs)
        )
        self._last_activity = datetime.now()
        return sms.sid

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="twilio_sms",
            account_id=self._phone_number,
            last_activity=self._last_activity,
        )

    async def _run_webhook_server(self) -> None:
        from aiohttp import web

        app = web.Application()
        app.router.add_post(self._webhook_path, self._handle_sms)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._webhook_host, self._webhook_port)
        await site.start()
        logger.info("twilio sms webhook listening on :%d", self._webhook_port)

        try:
            while self._connected:
                await asyncio.sleep(1)
        finally:
            await runner.cleanup()

    async def _handle_sms(self, request) -> None:
        from aiohttp import web

        data = await request.post()
        msg_sid = data.get("MessageSid", "")
        sender = data.get("From", "")
        body = data.get("Body", "")
        num_media = int(data.get("NumMedia", "0"))

        content_type = ContentType.TEXT
        media_urls = []
        if num_media > 0:
            content_type = ContentType.IMAGE
            for i in range(num_media):
                url = data.get(f"MediaUrl{i}", "")
                if url:
                    media_urls.append(url)

        self._last_activity = datetime.now()

        msg = UnifiedMessage(
            id=msg_sid,
            channel="twilio_sms",
            sender=Identity(id=sender, username=sender, display_name=sender),
            content=MessageContent(type=content_type, text=body, url=media_urls[0] if media_urls else None),
            chat_id=sender,
            raw=dict(data),
        )
        await self._queue.put(msg)

        twiml = "<Response></Response>"
        return web.Response(text=twiml, content_type="application/xml")
