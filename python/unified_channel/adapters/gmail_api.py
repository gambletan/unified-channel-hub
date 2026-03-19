"""Gmail API adapter — richer email functionality via Google Gmail API.

Uses OAuth2 for authentication, supports labels, threads, and attachments.

Requires: pip install google-auth google-auth-oauthlib google-api-python-client
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

_DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailAPIAdapter(ChannelAdapter):
    """Gmail adapter using the Google Gmail API (not IMAP)."""

    channel_id = "gmail"

    def __init__(
        self,
        credentials_path: str,
        *,
        token_path: str | None = None,
        scopes: list[str] | None = None,
        poll_interval: float = 30.0,
        label_ids: list[str] | None = None,
    ) -> None:
        self._credentials_path = credentials_path
        self._token_path = token_path or "gmail_token.json"
        self._scopes = scopes or list(_DEFAULT_SCOPES)
        self._poll_interval = poll_interval
        self._label_ids = label_ids or ["INBOX"]

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._poll_task: asyncio.Task | None = None
        self._service = None
        self._seen_ids: set[str] = set()
        self._user_email: str | None = None

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._service = await loop.run_in_executor(None, self._build_service)
        # Fetch authenticated user's email
        profile = await loop.run_in_executor(
            None, lambda: self._service.users().getProfile(userId="me").execute()
        )
        self._user_email = profile.get("emailAddress")
        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("gmail connected: %s", self._user_email)

    def _build_service(self):
        import os

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(self._token_path):
            creds = Credentials.from_authorized_user_file(self._token_path, self._scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_path, self._scopes
                )
                creds = flow.run_local_server(port=0)
            with open(self._token_path, "w") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task:
            self._poll_task.cancel()
        logger.info("gmail disconnected: %s", self._user_email)

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await self._queue.get()
                yield msg
            except asyncio.CancelledError:
                break

    async def send(self, msg: OutboundMessage) -> str | None:
        """Send an email via Gmail API.

        Expected msg fields:
        - chat_id: recipient email address
        - text: email body
        - metadata.subject: email subject line
        - metadata.cc: optional CC addresses (comma-separated)
        - metadata.bcc: optional BCC addresses (comma-separated)
        - metadata.thread_id: optional Gmail thread ID for replies
        """
        if not self._service:
            raise RuntimeError("gmail not connected")

        meta = msg.metadata or {}
        mime = MIMEMultipart()
        mime["To"] = msg.chat_id
        mime["Subject"] = meta.get("subject", "")
        if self._user_email:
            mime["From"] = self._user_email
        if meta.get("cc"):
            mime["Cc"] = meta["cc"]
        if meta.get("bcc"):
            mime["Bcc"] = meta["bcc"]
        mime.attach(MIMEText(msg.text, "plain"))

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        body: dict = {"raw": raw}
        if meta.get("thread_id"):
            body["threadId"] = meta["thread_id"]

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._service.users().messages().send(userId="me", body=body).execute(),
        )
        self._last_activity = datetime.now()
        return result.get("id")

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="gmail",
            account_id=self._user_email or "unknown",
            last_activity=self._last_activity,
        )

    async def _poll_loop(self) -> None:
        while self._connected:
            try:
                loop = asyncio.get_running_loop()
                messages = await loop.run_in_executor(None, self._fetch_unread)
                for m in messages:
                    await self._queue.put(m)
            except Exception as e:
                logger.warning("gmail poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    def _fetch_unread(self) -> list[UnifiedMessage]:
        if not self._service:
            return []
        results: list[UnifiedMessage] = []

        try:
            resp = (
                self._service.users()
                .messages()
                .list(userId="me", q="is:unread", labelIds=self._label_ids, maxResults=20)
                .execute()
            )
            for item in resp.get("messages", []):
                msg_id = item["id"]
                if msg_id in self._seen_ids:
                    continue
                self._seen_ids.add(msg_id)

                detail = (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )
                headers = {h["name"].lower(): h["value"] for h in detail.get("payload", {}).get("headers", [])}
                sender_raw = headers.get("from", "unknown")
                subject = headers.get("subject", "")
                thread_id = detail.get("threadId")
                label_ids = detail.get("labelIds", [])

                # Extract sender name and email
                sender_name, sender_email = self._parse_sender(sender_raw)
                body = self._extract_body(detail.get("payload", {}))

                self._last_activity = datetime.now()
                unified = UnifiedMessage(
                    id=msg_id,
                    channel="gmail",
                    sender=Identity(
                        id=sender_email,
                        username=sender_email,
                        display_name=sender_name or sender_email,
                    ),
                    content=MessageContent(type=ContentType.TEXT, text=body),
                    chat_id=sender_email,
                    raw={
                        "subject": subject,
                        "from": sender_raw,
                        "to": headers.get("to", ""),
                        "thread_id": thread_id,
                        "label_ids": label_ids,
                        "has_attachments": self._has_attachments(detail.get("payload", {})),
                    },
                )
                results.append(unified)
        except Exception as e:
            logger.warning("gmail fetch error: %s", e)

        return results

    @staticmethod
    def _parse_sender(raw: str) -> tuple[str, str]:
        """Parse 'Display Name <email@example.com>' into (name, email)."""
        if "<" in raw and ">" in raw:
            name = raw[: raw.index("<")].strip().strip('"')
            addr = raw[raw.index("<") + 1 : raw.index(">")]
            return name, addr
        return "", raw.strip()

    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Extract plain text body from Gmail message payload."""
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")

        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode(errors="replace")
            # Check nested multipart
            if part.get("parts"):
                result = GmailAPIAdapter._extract_body(part)
                if result:
                    return result
        return ""

    @staticmethod
    def _has_attachments(payload: dict) -> bool:
        """Check if message has any attachments."""
        for part in payload.get("parts", []):
            if part.get("filename"):
                return True
            if part.get("parts") and GmailAPIAdapter._has_attachments(part):
                return True
        return False
