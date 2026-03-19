"""Email adapter — IMAP polling for inbound, SMTP for outbound.

Supports Gmail, Outlook, and any standard IMAP/SMTP server.
No extra dependencies required (uses Python stdlib imaplib/smtplib).

For Gmail: enable "App Passwords" or use OAuth2 token.
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from imaplib import IMAP4_SSL
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

# Common IMAP/SMTP presets
_PRESETS = {
    "gmail": {"imap": "imap.gmail.com", "smtp": "smtp.gmail.com", "smtp_port": 587},
    "outlook": {"imap": "outlook.office365.com", "smtp": "smtp.office365.com", "smtp_port": 587},
}


class EmailAdapter(ChannelAdapter):
    """Email adapter using IMAP (receive) and SMTP (send)."""

    channel_id = "email"

    def __init__(
        self,
        email_address: str,
        password: str,
        *,
        imap_host: str | None = None,
        smtp_host: str | None = None,
        smtp_port: int = 587,
        preset: str | None = None,
        poll_interval: float = 30.0,
        mailbox: str = "INBOX",
    ) -> None:
        self._email = email_address
        self._password = password
        self._mailbox = mailbox
        self._poll_interval = poll_interval

        if preset and preset.lower() in _PRESETS:
            p = _PRESETS[preset.lower()]
            self._imap_host = imap_host or p["imap"]
            self._smtp_host = smtp_host or p["smtp"]
            self._smtp_port = smtp_port or p["smtp_port"]
        else:
            self._imap_host = imap_host or ""
            self._smtp_host = smtp_host or ""
            self._smtp_port = smtp_port

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._poll_task: asyncio.Task | None = None
        self._seen_uids: set[str] = set()
        self._imap: IMAP4_SSL | None = None

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._imap = await loop.run_in_executor(None, self._connect_imap)
        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("email connected: %s via %s", self._email, self._imap_host)

    def _connect_imap(self) -> IMAP4_SSL:
        conn = IMAP4_SSL(self._imap_host)
        conn.login(self._email, self._password)
        conn.select(self._mailbox)
        return conn

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._imap:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._imap.logout)
            except Exception:
                pass
        logger.info("email disconnected: %s", self._email)

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await self._queue.get()
                yield msg
            except asyncio.CancelledError:
                break

    async def send(self, msg: OutboundMessage) -> str | None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_smtp, msg)
        self._last_activity = datetime.now()
        return None

    def _send_smtp(self, msg: OutboundMessage) -> None:
        mime = MIMEMultipart()
        mime["From"] = self._email
        mime["To"] = msg.chat_id  # recipient email
        mime["Subject"] = msg.metadata.get("subject", "") if msg.metadata else ""
        mime.attach(MIMEText(msg.text, "plain"))

        with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
            server.starttls()
            server.login(self._email, self._password)
            server.send_message(mime)

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="email",
            account_id=self._email,
            last_activity=self._last_activity,
        )

    async def _poll_loop(self) -> None:
        while self._connected:
            try:
                loop = asyncio.get_running_loop()
                messages = await loop.run_in_executor(None, self._fetch_new)
                for m in messages:
                    await self._queue.put(m)
            except Exception as e:
                logger.warning("email poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    def _fetch_new(self) -> list[UnifiedMessage]:
        if not self._imap:
            return []
        results: list[UnifiedMessage] = []
        try:
            self._imap.noop()  # keep alive
            _, data = self._imap.search(None, "UNSEEN")
            uids = data[0].split() if data[0] else []
            for uid in uids:
                uid_str = uid.decode()
                if uid_str in self._seen_uids:
                    continue
                self._seen_uids.add(uid_str)
                _, msg_data = self._imap.fetch(uid, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                raw_email = msg_data[0][1]
                parsed = email.message_from_bytes(raw_email)
                sender_name, sender_addr = email.utils.parseaddr(parsed["From"])
                subject = parsed.get("Subject", "")
                body = self._extract_body(parsed)
                self._last_activity = datetime.now()
                msg = UnifiedMessage(
                    id=uid_str,
                    channel="email",
                    sender=Identity(
                        id=sender_addr,
                        username=sender_addr,
                        display_name=sender_name or sender_addr,
                    ),
                    content=MessageContent(type=ContentType.TEXT, text=body),
                    chat_id=sender_addr,
                    raw={"subject": subject, "from": parsed["From"], "to": parsed["To"]},
                )
                results.append(msg)
        except Exception as e:
            logger.warning("email fetch error: %s", e)
        return results

    @staticmethod
    def _extract_body(msg: email.message.Message) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(errors="replace")
            return ""
        payload = msg.get_payload(decode=True)
        return payload.decode(errors="replace") if payload else ""
