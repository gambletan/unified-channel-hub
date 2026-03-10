"""Tests for OutlookAdapter."""

from __future__ import annotations

import pytest

from unified_channel.adapters.outlook import OutlookAdapter


def test_outlook_channel_id():
    adapter = OutlookAdapter("client-id", "client-secret", "tenant-id")
    assert adapter.channel_id == "outlook"


def test_outlook_defaults():
    adapter = OutlookAdapter("client-id", "client-secret", "tenant-id")
    assert adapter._token_path == "outlook_token.json"
    assert adapter._poll_interval == 30.0
    assert adapter._redirect_uri == "http://localhost:8400"
    assert adapter._connected is False
    assert adapter._http is None


def test_outlook_custom_options():
    adapter = OutlookAdapter(
        "client-id",
        "client-secret",
        "tenant-id",
        redirect_uri="http://localhost:9999",
        token_path="/custom/token.json",
        poll_interval=120.0,
    )
    assert adapter._redirect_uri == "http://localhost:9999"
    assert adapter._token_path == "/custom/token.json"
    assert adapter._poll_interval == 120.0


def test_outlook_default_scopes():
    adapter = OutlookAdapter("client-id", "client-secret", "tenant-id")
    assert "https://graph.microsoft.com/.default" in adapter._scopes


def test_outlook_custom_scopes():
    custom = ["Mail.Read", "Mail.Send"]
    adapter = OutlookAdapter("client-id", "client-secret", "tenant-id", scopes=custom)
    assert adapter._scopes == custom


def test_outlook_stores_credentials():
    adapter = OutlookAdapter("my-client-id", "my-secret", "my-tenant")
    assert adapter._client_id == "my-client-id"
    assert adapter._client_secret == "my-secret"
    assert adapter._tenant_id == "my-tenant"


@pytest.mark.asyncio
async def test_outlook_status_disconnected():
    adapter = OutlookAdapter("client-id", "client-secret", "tenant-id")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "outlook"
    assert status.account_id == "unknown"
    assert status.last_activity is None


@pytest.mark.asyncio
async def test_outlook_send_not_connected():
    from unified_channel.types import OutboundMessage
    adapter = OutlookAdapter("client-id", "client-secret", "tenant-id")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send(OutboundMessage(chat_id="test@example.com", text="Hello"))


@pytest.mark.asyncio
async def test_outlook_create_event_not_connected():
    adapter = OutlookAdapter("client-id", "client-secret", "tenant-id")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.create_event("Meeting", "2026-03-11T10:00:00", "2026-03-11T11:00:00")


@pytest.mark.asyncio
async def test_outlook_list_events_not_connected():
    adapter = OutlookAdapter("client-id", "client-secret", "tenant-id")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.list_events()
