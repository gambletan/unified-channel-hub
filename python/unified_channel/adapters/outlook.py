"""Outlook adapter — Microsoft Graph API for email and calendar.

Uses MSAL for OAuth2 authentication, httpx for HTTP requests.
Supports both email (inbox polling, send) and calendar (create/list events).

Requires: pip install httpx msal
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, AsyncIterator

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

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_SCOPES = ["https://graph.microsoft.com/.default"]
_AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}"


class OutlookAdapter(ChannelAdapter):
    """Outlook adapter using Microsoft Graph API — email + calendar."""

    channel_id = "outlook"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        *,
        redirect_uri: str = "http://localhost:8400",
        token_path: str | None = None,
        poll_interval: float = 30.0,
        scopes: list[str] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._tenant_id = tenant_id
        self._redirect_uri = redirect_uri
        self._token_path = token_path or "outlook_token.json"
        self._poll_interval = poll_interval
        self._scopes = scopes or list(_DEFAULT_SCOPES)

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._poll_task: asyncio.Task | None = None
        self._http = None  # httpx.AsyncClient
        self._access_token: str | None = None
        self._seen_ids: set[str] = set()
        self._user_email: str | None = None

    async def connect(self) -> None:
        import httpx
        import msal

        loop = asyncio.get_running_loop()
        self._access_token = await loop.run_in_executor(None, self._acquire_token)
        self._http = httpx.AsyncClient(
            base_url=_GRAPH_BASE,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=30.0,
        )

        # Fetch user profile
        resp = await self._http.get("/me")
        resp.raise_for_status()
        profile = resp.json()
        self._user_email = profile.get("mail") or profile.get("userPrincipalName")

        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("outlook connected: %s", self._user_email)

    def _acquire_token(self) -> str:
        import os

        import msal

        authority = _AUTHORITY_TEMPLATE.format(tenant_id=self._tenant_id)
        app = msal.ConfidentialClientApplication(
            self._client_id,
            authority=authority,
            client_credential=self._client_secret,
        )

        # Try cached token first
        if os.path.exists(self._token_path):
            with open(self._token_path) as f:
                cache_data = json.load(f)
            if "access_token" in cache_data:
                return cache_data["access_token"]

        # Acquire token via client credentials flow
        result = app.acquire_token_for_client(scopes=self._scopes)
        if "access_token" not in result:
            raise RuntimeError(f"outlook auth failed: {result.get('error_description', result.get('error'))}")

        # Cache the token
        with open(self._token_path, "w") as f:
            json.dump({"access_token": result["access_token"]}, f)

        return result["access_token"]

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("outlook disconnected: %s", self._user_email)

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, msg: OutboundMessage) -> str | None:
        """Send an email via Microsoft Graph API.

        Expected msg fields:
        - chat_id: recipient email address
        - text: email body
        - metadata.subject: email subject line
        - metadata.cc: optional list of CC email addresses
        - metadata.content_type: "HTML" or "Text" (default "Text")
        """
        if not self._http:
            raise RuntimeError("outlook not connected")

        meta = msg.metadata or {}
        content_type = meta.get("content_type", "Text")
        body: dict[str, Any] = {
            "message": {
                "subject": meta.get("subject", ""),
                "body": {"contentType": content_type, "content": msg.text},
                "toRecipients": [{"emailAddress": {"address": msg.chat_id}}],
            }
        }
        if meta.get("cc"):
            body["message"]["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in meta["cc"]
            ]

        resp = await self._http.post("/me/sendMail", json=body)
        resp.raise_for_status()
        self._last_activity = datetime.now()
        return None  # Graph sendMail doesn't return message ID

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="outlook",
            account_id=self._user_email or "unknown",
            last_activity=self._last_activity,
        )

    # ---- Calendar helpers ----

    async def create_event(
        self,
        subject: str,
        start: str,
        end: str,
        *,
        body: str | None = None,
        attendees: list[str] | None = None,
        timezone: str = "UTC",
    ) -> dict:
        """Create a calendar event via Graph API."""
        if not self._http:
            raise RuntimeError("outlook not connected")

        event_body: dict[str, Any] = {
            "subject": subject,
            "start": {"dateTime": start, "timeZone": timezone},
            "end": {"dateTime": end, "timeZone": timezone},
        }
        if body:
            event_body["body"] = {"contentType": "Text", "content": body}
        if attendees:
            event_body["attendees"] = [
                {"emailAddress": {"address": addr}, "type": "required"} for addr in attendees
            ]

        resp = await self._http.post("/me/events", json=event_body)
        resp.raise_for_status()
        return resp.json()

    async def list_events(self, *, top: int = 10, filter_query: str | None = None) -> list[dict]:
        """List upcoming calendar events."""
        if not self._http:
            raise RuntimeError("outlook not connected")

        params: dict[str, Any] = {"$top": top, "$orderby": "start/dateTime"}
        if filter_query:
            params["$filter"] = filter_query

        resp = await self._http.get("/me/events", params=params)
        resp.raise_for_status()
        return resp.json().get("value", [])

    # ---- Polling ----

    async def _poll_loop(self) -> None:
        while self._connected:
            try:
                messages = await self._fetch_unread()
                for m in messages:
                    await self._queue.put(m)
            except Exception as e:
                logger.warning("outlook poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    async def _fetch_unread(self) -> list[UnifiedMessage]:
        if not self._http:
            return []
        results: list[UnifiedMessage] = []

        try:
            resp = await self._http.get(
                "/me/mailFolders/inbox/messages",
                params={"$filter": "isRead eq false", "$top": 20},
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("value", []):
                msg_id = item["id"]
                if msg_id in self._seen_ids:
                    continue
                self._seen_ids.add(msg_id)

                sender_info = item.get("from", {}).get("emailAddress", {})
                sender_email = sender_info.get("address", "unknown")
                sender_name = sender_info.get("name", sender_email)
                subject = item.get("subject", "")
                body_content = item.get("body", {}).get("content", "")
                conversation_id = item.get("conversationId")
                has_attachments = item.get("hasAttachments", False)

                self._last_activity = datetime.now()
                unified = UnifiedMessage(
                    id=msg_id,
                    channel="outlook",
                    sender=Identity(
                        id=sender_email,
                        username=sender_email,
                        display_name=sender_name,
                    ),
                    content=MessageContent(type=ContentType.TEXT, text=body_content),
                    chat_id=sender_email,
                    raw={
                        "subject": subject,
                        "from": sender_email,
                        "conversation_id": conversation_id,
                        "has_attachments": has_attachments,
                        "importance": item.get("importance", "normal"),
                    },
                )
                results.append(unified)
        except Exception as e:
            logger.warning("outlook fetch error: %s", e)

        return results
