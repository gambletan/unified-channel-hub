"""Tests for new adapters — TwilioVoice, TwilioSMS, GoogleCalendar, HomeAssistant."""

from __future__ import annotations

import pytest

from unified_channel.adapters.twilio_voice import TwilioVoiceAdapter
from unified_channel.adapters.twilio_sms import TwilioSMSAdapter
from unified_channel.adapters.google_calendar import GoogleCalendarAdapter
from unified_channel.adapters.homeassistant import HomeAssistantAdapter


# ---- TwilioVoiceAdapter ----

def test_twilio_voice_channel_id():
    adapter = TwilioVoiceAdapter("AC123", "token", "+15551234567")
    assert adapter.channel_id == "twilio_voice"


def test_twilio_voice_defaults():
    adapter = TwilioVoiceAdapter("AC123", "token", "+15551234567")
    assert adapter._webhook_port == 8080
    assert adapter._webhook_path == "/voice"
    assert adapter._connected is False


def test_twilio_voice_custom_port():
    adapter = TwilioVoiceAdapter("AC123", "token", "+15551234567", webhook_port=9090)
    assert adapter._webhook_port == 9090


@pytest.mark.asyncio
async def test_twilio_voice_status_disconnected():
    adapter = TwilioVoiceAdapter("AC123", "token", "+15551234567")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "twilio_voice"
    assert status.account_id == "+15551234567"


@pytest.mark.asyncio
async def test_twilio_voice_send_not_connected():
    from unified_channel.types import OutboundMessage
    adapter = TwilioVoiceAdapter("AC123", "token", "+15551234567")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send(OutboundMessage(chat_id="+15559999999", text="Hello"))


# ---- TwilioSMSAdapter ----

def test_twilio_sms_channel_id():
    adapter = TwilioSMSAdapter("AC123", "token", "+15551234567")
    assert adapter.channel_id == "twilio_sms"


def test_twilio_sms_defaults():
    adapter = TwilioSMSAdapter("AC123", "token", "+15551234567")
    assert adapter._webhook_port == 8081
    assert adapter._webhook_path == "/sms"


@pytest.mark.asyncio
async def test_twilio_sms_status():
    adapter = TwilioSMSAdapter("AC123", "token", "+15551234567")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "twilio_sms"


@pytest.mark.asyncio
async def test_twilio_sms_send_not_connected():
    from unified_channel.types import OutboundMessage
    adapter = TwilioSMSAdapter("AC123", "token", "+15551234567")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send(OutboundMessage(chat_id="+15559999999", text="Hi"))


# ---- GoogleCalendarAdapter ----

def test_google_calendar_channel_id():
    adapter = GoogleCalendarAdapter("/fake/creds.json")
    assert adapter.channel_id == "google_calendar"


def test_google_calendar_defaults():
    adapter = GoogleCalendarAdapter("/fake/creds.json")
    assert adapter._calendar_id == "primary"
    assert adapter._poll_interval == 60.0
    assert adapter._connected is False


def test_google_calendar_custom_options():
    adapter = GoogleCalendarAdapter(
        "/fake/creds.json",
        calendar_id="work@group.calendar.google.com",
        poll_interval=120.0,
    )
    assert adapter._calendar_id == "work@group.calendar.google.com"
    assert adapter._poll_interval == 120.0


@pytest.mark.asyncio
async def test_google_calendar_status():
    adapter = GoogleCalendarAdapter("/fake/creds.json")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "google_calendar"
    assert status.account_id == "primary"


@pytest.mark.asyncio
async def test_google_calendar_send_not_connected():
    from unified_channel.types import OutboundMessage
    adapter = GoogleCalendarAdapter("/fake/creds.json")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send(OutboundMessage(
            chat_id="primary", text="Meeting",
            metadata={"start": "2026-03-11T10:00:00Z", "end": "2026-03-11T11:00:00Z"},
        ))


# ---- HomeAssistantAdapter ----

def test_homeassistant_channel_id():
    adapter = HomeAssistantAdapter("http://ha.local:8123", "token123")
    assert adapter.channel_id == "homeassistant"


def test_homeassistant_defaults():
    adapter = HomeAssistantAdapter("http://ha.local:8123", "token123")
    assert adapter._url == "http://ha.local:8123"
    assert adapter._ws_url == "ws://ha.local:8123/api/websocket"
    assert adapter._entity_filters is None
    assert adapter._connected is False


def test_homeassistant_trailing_slash():
    adapter = HomeAssistantAdapter("http://ha.local:8123/", "token123")
    assert adapter._url == "http://ha.local:8123"


def test_homeassistant_entity_filters():
    adapter = HomeAssistantAdapter(
        "http://ha.local:8123", "token123",
        entity_filters=["light.", "switch."],
    )
    assert adapter._should_include("light.living_room") is True
    assert adapter._should_include("switch.kitchen") is True
    assert adapter._should_include("sensor.temperature") is False


def test_homeassistant_no_filter():
    adapter = HomeAssistantAdapter("http://ha.local:8123", "token123")
    assert adapter._should_include("anything.goes") is True


@pytest.mark.asyncio
async def test_homeassistant_status():
    adapter = HomeAssistantAdapter("http://ha.local:8123", "token123")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "homeassistant"
    assert status.account_id == "http://ha.local:8123"


def test_homeassistant_ws_url_https():
    adapter = HomeAssistantAdapter("https://ha.example.com", "token123")
    assert adapter._ws_url == "wss://ha.example.com/api/websocket"
