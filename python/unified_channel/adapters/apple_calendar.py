"""Apple Calendar (CalDAV) adapter — receive events and create new ones.

Requires: pip install httpx

Uses CalDAV protocol (HTTP + XML) to communicate with iCloud Calendar
or any CalDAV-compatible server.

iCloud CalDAV URL: https://caldav.icloud.com
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from xml.etree import ElementTree as ET

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

# CalDAV XML namespaces
DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
ICAL_NS = "http://apple.com/ns/ical/"

NS_MAP = {
    "D": DAV_NS,
    "C": CALDAV_NS,
}


def format_ics_event(
    *,
    uid: str,
    summary: str,
    dtstart: str,
    dtend: str,
    description: str = "",
    location: str = "",
    organizer: str = "",
) -> str:
    """Format a VCALENDAR/VEVENT in ICS format.

    Args:
        uid: Unique event identifier.
        summary: Event title.
        dtstart: Start time in ISO format (will be converted to ICS format).
        dtend: End time in ISO format (will be converted to ICS format).
        description: Optional event description.
        location: Optional location string.
        organizer: Optional organizer email.

    Returns:
        ICS-formatted string.
    """
    # Convert ISO datetime to ICS format: 20260310T100000Z
    start_ics = _iso_to_ics(dtstart)
    end_ics = _iso_to_ics(dtend)
    now_ics = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//UnifiedChannel//AppleCalendarAdapter//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_ics}",
        f"DTSTART:{start_ics}",
        f"DTEND:{end_ics}",
        f"SUMMARY:{summary}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{description}")
    if location:
        lines.append(f"LOCATION:{location}")
    if organizer:
        lines.append(f"ORGANIZER:mailto:{organizer}")
    lines += [
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines)


def _iso_to_ics(iso_str: str) -> str:
    """Convert an ISO datetime string to ICS format (YYYYMMDDTHHmmSSZ)."""
    # Remove common separators and timezone info for basic conversion
    cleaned = iso_str.replace("-", "").replace(":", "")
    # Handle 'Z' suffix or +00:00
    if cleaned.endswith("Z"):
        return cleaned
    # If there's a timezone offset, strip it and add Z
    if "+" in cleaned or cleaned.count("-") > 0:
        # Remove timezone offset (last 4-5 chars like +0000)
        cleaned = re.sub(r"[+-]\d{4}$", "", cleaned)
    if not cleaned.endswith("Z"):
        cleaned += "Z"
    return cleaned


def _build_calendar_query_xml() -> str:
    """Build the REPORT XML body for fetching calendar events."""
    return """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT"/>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""


def _parse_vevent(ical_text: str) -> dict[str, str]:
    """Parse a VEVENT from ICS text into a flat dict of properties."""
    result: dict[str, str] = {}
    in_vevent = False
    for line in ical_text.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            in_vevent = True
            continue
        if line == "END:VEVENT":
            break
        if in_vevent and ":" in line:
            key, val = line.split(":", 1)
            # Strip parameters (e.g. DTSTART;VALUE=DATE:20260310)
            key = key.split(";")[0]
            result[key] = val
    return result


class AppleCalendarAdapter(ChannelAdapter):
    """Apple Calendar adapter using CalDAV protocol."""

    channel_id = "apple_calendar"

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        *,
        calendar_name: str = "default",
        poll_interval: float = 60.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._calendar_name = calendar_name
        self._poll_interval = poll_interval

        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._poll_task: asyncio.Task | None = None
        self._client: Any = None  # httpx.AsyncClient
        self._calendar_path: str | None = None
        self._seen_etags: set[str] = set()

    async def connect(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            auth=(self._username, self._password),
            timeout=30.0,
            follow_redirects=True,
        )

        # Discover calendar path via PROPFIND
        await self._discover_calendar()
        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "apple calendar connected: %s (calendar: %s)",
            self._url,
            self._calendar_name,
        )

    async def _discover_calendar(self) -> None:
        """Discover the calendar collection path using PROPFIND."""
        propfind_body = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:displayname/>
    <D:resourcetype/>
    <C:calendar-home-set/>
  </D:prop>
</D:propfind>"""

        if not self._client:
            return

        try:
            resp = await self._client.request(
                "PROPFIND",
                f"{self._url}/",
                content=propfind_body,
                headers={
                    "Content-Type": "application/xml",
                    "Depth": "1",
                },
            )
            if resp.status_code in (200, 207):
                # Use URL path as calendar path fallback
                self._calendar_path = f"{self._url}/calendars/{self._username}/{self._calendar_name}/"
            else:
                logger.warning("PROPFIND failed with status %d, using default path", resp.status_code)
                self._calendar_path = f"{self._url}/calendars/{self._username}/{self._calendar_name}/"
        except Exception as e:
            logger.warning("calendar discovery failed: %s, using default path", e)
            self._calendar_path = f"{self._url}/calendars/{self._username}/{self._calendar_name}/"

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("apple calendar disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, msg: OutboundMessage) -> str | None:
        """Create a calendar event via CalDAV PUT.

        Expected msg.metadata keys:
        - summary: event title (or uses msg.text)
        - start: ISO datetime string
        - end: ISO datetime string
        - description: optional event description
        - location: optional location string
        """
        if not self._client or not self._connected:
            raise RuntimeError("apple calendar not connected")

        meta = msg.metadata or {}
        uid = str(uuid.uuid4())
        ics = format_ics_event(
            uid=uid,
            summary=meta.get("summary", msg.text),
            dtstart=meta["start"],
            dtend=meta["end"],
            description=meta.get("description", ""),
            location=meta.get("location", ""),
            organizer=self._username,
        )

        cal_path = self._calendar_path or f"{self._url}/calendars/{self._username}/{self._calendar_name}/"
        event_url = f"{cal_path}{uid}.ics"

        resp = await self._client.put(
            event_url,
            content=ics,
            headers={
                "Content-Type": "text/calendar; charset=utf-8",
                "If-None-Match": "*",
            },
        )

        if resp.status_code in (201, 204):
            self._last_activity = datetime.now()
            return uid
        else:
            logger.warning("failed to create event: %d %s", resp.status_code, resp.text)
            return None

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="apple_calendar",
            account_id=f"{self._username}@{self._url}",
            last_activity=self._last_activity,
        )

    async def _poll_loop(self) -> None:
        """Poll for calendar events using REPORT request."""
        while self._connected:
            try:
                events = await self._fetch_events()
                for ev in events:
                    await self._queue.put(ev)
            except Exception as e:
                logger.warning("calendar poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    async def _fetch_events(self) -> list[UnifiedMessage]:
        """Fetch events using CalDAV REPORT with calendar-query."""
        if not self._client or not self._calendar_path:
            return []

        results: list[UnifiedMessage] = []
        query_xml = _build_calendar_query_xml()

        resp = await self._client.request(
            "REPORT",
            self._calendar_path,
            content=query_xml,
            headers={
                "Content-Type": "application/xml",
                "Depth": "1",
            },
        )

        if resp.status_code not in (200, 207):
            logger.warning("REPORT failed with status %d", resp.status_code)
            return results

        # Parse multistatus XML response
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            logger.warning("failed to parse CalDAV response: %s", e)
            return results

        for response_el in root.findall(f"{{{DAV_NS}}}response"):
            href = response_el.findtext(f"{{{DAV_NS}}}href", "")
            etag_el = response_el.find(f".//{{{DAV_NS}}}getetag")
            etag = etag_el.text if etag_el is not None and etag_el.text else ""

            if etag and etag in self._seen_etags:
                continue
            if etag:
                self._seen_etags.add(etag)

            cal_data_el = response_el.find(f".//{{{CALDAV_NS}}}calendar-data")
            if cal_data_el is None or not cal_data_el.text:
                continue

            vevent = _parse_vevent(cal_data_el.text)
            if not vevent:
                continue

            event_uid = vevent.get("UID", href)
            summary = vevent.get("SUMMARY", "(No title)")
            dtstart = vevent.get("DTSTART", "")
            dtend = vevent.get("DTEND", "")
            description = vevent.get("DESCRIPTION", "")
            organizer = vevent.get("ORGANIZER", "").replace("mailto:", "")

            text = f"{summary}\n{dtstart} — {dtend}"
            if description:
                text += f"\n{description}"

            self._last_activity = datetime.now()

            msg = UnifiedMessage(
                id=event_uid,
                channel="apple_calendar",
                sender=Identity(
                    id=organizer or self._username,
                    username=organizer or self._username,
                    display_name=organizer or self._username,
                ),
                content=MessageContent(type=ContentType.TEXT, text=text),
                chat_id=self._calendar_name,
                raw=cal_data_el.text,
            )
            results.append(msg)

        return results
