"""Google Calendar adapter — receive event notifications, create/update events.

Requires: pip install google-auth google-auth-oauthlib google-api-python-client

Uses Google Calendar API v3 with push notifications or polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
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


class GoogleCalendarAdapter(ChannelAdapter):
    """Google Calendar adapter — events as messages."""

    channel_id = "google_calendar"

    def __init__(
        self,
        credentials_path: str,
        *,
        calendar_id: str = "primary",
        poll_interval: float = 60.0,
        token_path: str | None = None,
        scopes: list[str] | None = None,
    ) -> None:
        self._credentials_path = credentials_path
        self._calendar_id = calendar_id
        self._poll_interval = poll_interval
        self._token_path = token_path or "calendar_token.json"
        self._scopes = scopes or ["https://www.googleapis.com/auth/calendar"]

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._poll_task: asyncio.Task | None = None
        self._service = None
        self._seen_ids: set[str] = set()
        self._last_sync: str | None = None

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._service = await loop.run_in_executor(None, self._build_service)
        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("google calendar connected: %s", self._calendar_id)

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

        return build("calendar", "v3", credentials=creds)

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task:
            self._poll_task.cancel()
        logger.info("google calendar disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await self._queue.get()
                yield msg
            except asyncio.CancelledError:
                break

    async def send(self, msg: OutboundMessage) -> str | None:
        """Create a calendar event from an outbound message.

        Expected msg.metadata keys:
        - summary: event title (or uses msg.text)
        - start: ISO datetime string
        - end: ISO datetime string
        - description: optional event description
        - attendees: optional list of email addresses
        """
        if not self._service:
            raise RuntimeError("google calendar not connected")

        meta = msg.metadata or {}
        event_body = {
            "summary": meta.get("summary", msg.text),
            "description": meta.get("description", msg.text),
            "start": {"dateTime": meta["start"], "timeZone": meta.get("timezone", "UTC")},
            "end": {"dateTime": meta["end"], "timeZone": meta.get("timezone", "UTC")},
        }
        if meta.get("attendees"):
            event_body["attendees"] = [{"email": e} for e in meta["attendees"]]

        loop = asyncio.get_running_loop()
        event = await loop.run_in_executor(
            None,
            lambda: self._service.events()
            .insert(calendarId=self._calendar_id, body=event_body)
            .execute(),
        )
        self._last_activity = datetime.now()
        return event.get("id")

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="google_calendar",
            account_id=self._calendar_id,
            last_activity=self._last_activity,
        )

    async def _poll_loop(self) -> None:
        while self._connected:
            try:
                loop = asyncio.get_running_loop()
                events = await loop.run_in_executor(None, self._fetch_upcoming)
                for ev in events:
                    await self._queue.put(ev)
            except Exception as e:
                logger.warning("calendar poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    def _fetch_upcoming(self) -> list[UnifiedMessage]:
        if not self._service:
            return []
        results: list[UnifiedMessage] = []

        now = datetime.now(timezone.utc).isoformat()
        kwargs = {
            "calendarId": self._calendar_id,
            "timeMin": now,
            "maxResults": 10,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if self._last_sync:
            kwargs["updatedMin"] = self._last_sync

        resp = self._service.events().list(**kwargs).execute()
        self._last_sync = now

        for item in resp.get("items", []):
            event_id = item["id"]
            if event_id in self._seen_ids:
                continue
            self._seen_ids.add(event_id)
            self._last_activity = datetime.now()

            start = item.get("start", {}).get("dateTime", item.get("start", {}).get("date", ""))
            end = item.get("end", {}).get("dateTime", item.get("end", {}).get("date", ""))
            summary = item.get("summary", "(No title)")
            creator = item.get("creator", {})

            text = f"{summary}\n{start} — {end}"
            if item.get("description"):
                text += f"\n{item['description']}"

            msg = UnifiedMessage(
                id=event_id,
                channel="google_calendar",
                sender=Identity(
                    id=creator.get("email", "calendar"),
                    username=creator.get("email", "calendar"),
                    display_name=creator.get("displayName", creator.get("email", "Calendar")),
                ),
                content=MessageContent(type=ContentType.TEXT, text=text),
                chat_id=self._calendar_id,
                raw=item,
            )
            results.append(msg)

        return results
