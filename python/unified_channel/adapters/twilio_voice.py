"""Twilio Voice adapter — inbound/outbound phone calls via Twilio.

Requires: pip install twilio aiohttp

Inbound: Twilio webhook → local aiohttp server → UnifiedMessage
Outbound: REST API call initiation
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


class TwilioVoiceAdapter(ChannelAdapter):
    """Twilio Voice adapter for phone call interactions."""

    channel_id = "twilio_voice"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        phone_number: str,
        *,
        webhook_host: str = "0.0.0.0",
        webhook_port: int = 8080,
        webhook_path: str = "/voice",
        status_path: str = "/voice/status",
        twiml_response: str | None = None,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._phone_number = phone_number
        self._webhook_host = webhook_host
        self._webhook_port = webhook_port
        self._webhook_path = webhook_path
        self._status_path = status_path
        self._twiml_response = twiml_response

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._server_task: asyncio.Task | None = None
        self._client = None  # lazy twilio.rest.Client

    async def connect(self) -> None:
        from twilio.rest import Client

        self._client = Client(self._account_sid, self._auth_token)
        self._server_task = asyncio.create_task(self._run_webhook_server())
        self._connected = True
        logger.info("twilio voice connected: %s on :%d%s",
                     self._phone_number, self._webhook_port, self._webhook_path)

    async def disconnect(self) -> None:
        self._connected = False
        if self._server_task:
            self._server_task.cancel()
        logger.info("twilio voice disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, msg: OutboundMessage) -> str | None:
        if not self._client:
            raise RuntimeError("twilio voice not connected")

        loop = asyncio.get_running_loop()
        twiml = msg.metadata.get("twiml", "") if msg.metadata else ""
        if not twiml and msg.text:
            twiml = f"<Response><Say>{msg.text}</Say></Response>"

        call = await loop.run_in_executor(
            None,
            lambda: self._client.calls.create(
                to=msg.chat_id,
                from_=self._phone_number,
                twiml=twiml,
            ),
        )
        self._last_activity = datetime.now()
        return call.sid

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="twilio_voice",
            account_id=self._phone_number,
            last_activity=self._last_activity,
        )

    async def _run_webhook_server(self) -> None:
        from aiohttp import web

        app = web.Application()
        app.router.add_post(self._webhook_path, self._handle_voice)
        app.router.add_post(self._status_path, self._handle_status)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._webhook_host, self._webhook_port)
        await site.start()
        logger.info("twilio voice webhook listening on :%d", self._webhook_port)

        try:
            while self._connected:
                await asyncio.sleep(1)
        finally:
            await runner.cleanup()

    async def _handle_voice(self, request) -> None:
        from aiohttp import web

        data = await request.post()
        call_sid = data.get("CallSid", "")
        caller = data.get("From", "")
        called = data.get("To", "")
        speech_result = data.get("SpeechResult", "")
        digits = data.get("Digits", "")

        text = speech_result or digits or "[incoming call]"
        self._last_activity = datetime.now()

        msg = UnifiedMessage(
            id=call_sid,
            channel="twilio_voice",
            sender=Identity(id=caller, username=caller, display_name=caller),
            content=MessageContent(type=ContentType.TEXT, text=text),
            chat_id=caller,
            raw=dict(data),
        )
        await self._queue.put(msg)

        # Respond with TwiML
        twiml = self._twiml_response or "<Response><Say>Please hold.</Say></Response>"
        return web.Response(text=twiml, content_type="application/xml")

    async def _handle_status(self, request) -> None:
        from aiohttp import web

        data = await request.post()
        logger.debug("twilio voice status: %s", dict(data))
        return web.Response(text="OK")
