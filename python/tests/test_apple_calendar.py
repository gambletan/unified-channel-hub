"""Tests for AppleCalendarAdapter."""

from __future__ import annotations

import pytest

from unified_channel.adapters.apple_calendar import (
    AppleCalendarAdapter,
    format_ics_event,
    _iso_to_ics,
    _parse_vevent,
    _build_calendar_query_xml,
)


# ---- channel_id ----

def test_apple_calendar_channel_id():
    adapter = AppleCalendarAdapter("https://caldav.icloud.com", "user@icloud.com", "app-password")
    assert adapter.channel_id == "apple_calendar"


# ---- constructor defaults ----

def test_apple_calendar_defaults():
    adapter = AppleCalendarAdapter("https://caldav.icloud.com", "user@icloud.com", "app-password")
    assert adapter._url == "https://caldav.icloud.com"
    assert adapter._username == "user@icloud.com"
    assert adapter._calendar_name == "default"
    assert adapter._poll_interval == 60.0
    assert adapter._connected is False


def test_apple_calendar_custom_options():
    adapter = AppleCalendarAdapter(
        "https://caldav.icloud.com",
        "user@icloud.com",
        "app-password",
        calendar_name="Work",
        poll_interval=120.0,
    )
    assert adapter._calendar_name == "Work"
    assert adapter._poll_interval == 120.0


def test_apple_calendar_trailing_slash():
    adapter = AppleCalendarAdapter("https://caldav.icloud.com/", "user@icloud.com", "pw")
    assert adapter._url == "https://caldav.icloud.com"


# ---- get_status ----

@pytest.mark.asyncio
async def test_apple_calendar_status_disconnected():
    adapter = AppleCalendarAdapter("https://caldav.icloud.com", "user@icloud.com", "app-password")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "apple_calendar"
    assert status.account_id == "user@icloud.com@https://caldav.icloud.com"


# ---- send raises when not connected ----

@pytest.mark.asyncio
async def test_apple_calendar_send_not_connected():
    from unified_channel.types import OutboundMessage

    adapter = AppleCalendarAdapter("https://caldav.icloud.com", "user@icloud.com", "app-password")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send(OutboundMessage(
            chat_id="default",
            text="Team Meeting",
            metadata={"start": "2026-03-11T10:00:00Z", "end": "2026-03-11T11:00:00Z"},
        ))


# ---- ICS event formatting ----

def test_format_ics_event():
    ics = format_ics_event(
        uid="test-uid-123",
        summary="Team Standup",
        dtstart="2026-03-11T10:00:00Z",
        dtend="2026-03-11T10:30:00Z",
        description="Daily sync",
        location="Room 42",
    )
    assert "BEGIN:VCALENDAR" in ics
    assert "BEGIN:VEVENT" in ics
    assert "UID:test-uid-123" in ics
    assert "SUMMARY:Team Standup" in ics
    assert "DESCRIPTION:Daily sync" in ics
    assert "LOCATION:Room 42" in ics
    assert "END:VEVENT" in ics
    assert "END:VCALENDAR" in ics


def test_format_ics_event_minimal():
    ics = format_ics_event(
        uid="min-uid",
        summary="Quick Chat",
        dtstart="2026-03-11T14:00:00Z",
        dtend="2026-03-11T14:15:00Z",
    )
    assert "SUMMARY:Quick Chat" in ics
    assert "DESCRIPTION" not in ics
    assert "LOCATION" not in ics


# ---- ISO to ICS conversion ----

def test_iso_to_ics_utc():
    assert _iso_to_ics("2026-03-11T10:00:00Z") == "20260311T100000Z"


def test_iso_to_ics_with_offset():
    result = _iso_to_ics("2026-03-11T10:00:00+0800")
    assert result.endswith("Z")
    assert "-" not in result.replace("Z", "")


# ---- VEVENT parsing ----

def test_parse_vevent():
    ical = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:abc-123\r\n"
        "SUMMARY:Lunch\r\n"
        "DTSTART:20260311T120000Z\r\n"
        "DTEND:20260311T130000Z\r\n"
        "DESCRIPTION:With team\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR"
    )
    result = _parse_vevent(ical)
    assert result["UID"] == "abc-123"
    assert result["SUMMARY"] == "Lunch"
    assert result["DTSTART"] == "20260311T120000Z"
    assert result["DESCRIPTION"] == "With team"


# ---- Calendar query XML ----

def test_calendar_query_xml():
    xml = _build_calendar_query_xml()
    assert "calendar-query" in xml
    assert "VCALENDAR" in xml
    assert "VEVENT" in xml
    assert "getetag" in xml
    assert "calendar-data" in xml
