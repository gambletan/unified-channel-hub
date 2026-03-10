"""Tests for SIPAdapter."""

from __future__ import annotations

import pytest

from unified_channel.adapters.sip import (
    SIPAdapter,
    SIPState,
    build_register_message,
    build_invite_message,
    build_bye_message,
    build_ack_message,
    parse_sip_uri,
)


# ---- channel_id ----

def test_sip_channel_id():
    adapter = SIPAdapter("sip:user@pbx.example.com", "alice", "secret")
    assert adapter.channel_id == "sip"


# ---- constructor defaults ----

def test_sip_defaults():
    adapter = SIPAdapter("sip:user@pbx.example.com", "alice", "secret")
    assert adapter._local_port == 5060
    assert adapter._codec == "PCMU"
    assert adapter._stun_server is None
    assert adapter._connected is False
    assert adapter._state == SIPState.IDLE


def test_sip_custom_options():
    adapter = SIPAdapter(
        "sip:user@pbx.example.com",
        "alice",
        "secret",
        local_port=5080,
        codec="G729",
        stun_server="stun:stun.example.com",
    )
    assert adapter._local_port == 5080
    assert adapter._codec == "G729"
    assert adapter._stun_server == "stun:stun.example.com"


# ---- get_status ----

@pytest.mark.asyncio
async def test_sip_status_disconnected():
    adapter = SIPAdapter("sip:user@pbx.example.com", "alice", "secret")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "sip"
    assert status.account_id == "sip:user@pbx.example.com"


# ---- send raises when not connected ----

@pytest.mark.asyncio
async def test_sip_send_not_connected():
    from unified_channel.types import OutboundMessage

    adapter = SIPAdapter("sip:user@pbx.example.com", "alice", "secret")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send(OutboundMessage(chat_id="sip:bob@pbx.example.com", text="Hello"))


# ---- SIP URI parsing ----

def test_parse_sip_uri_basic():
    result = parse_sip_uri("sip:alice@pbx.example.com")
    assert result["scheme"] == "sip"
    assert result["user"] == "alice"
    assert result["host"] == "pbx.example.com"
    assert result["port"] == "5060"


def test_parse_sip_uri_with_port():
    result = parse_sip_uri("sip:alice@pbx.example.com:5080")
    assert result["user"] == "alice"
    assert result["host"] == "pbx.example.com"
    assert result["port"] == "5080"


def test_parse_sips_uri():
    result = parse_sip_uri("sips:alice@secure.example.com")
    assert result["scheme"] == "sips"
    assert result["port"] == "5061"


def test_parse_sip_uri_invalid():
    with pytest.raises(ValueError, match="invalid SIP"):
        parse_sip_uri("http://example.com")


# ---- REGISTER message building ----

def test_build_register_message():
    msg = build_register_message(
        "sip:alice@pbx.example.com",
        "alice",
        call_id="test-call-id",
        local_port=5060,
    )
    assert msg.startswith("REGISTER sip:pbx.example.com SIP/2.0\r\n")
    assert "Call-ID: test-call-id" in msg
    assert "CSeq: 1 REGISTER" in msg
    assert "From: <sip:alice@pbx.example.com>" in msg
    assert "Expires: 3600" in msg
    assert "Content-Length: 0" in msg


# ---- INVITE message building ----

def test_build_invite_message():
    msg = build_invite_message(
        "sip:bob@pbx.example.com",
        "sip:alice@pbx.example.com",
        "alice",
        call_id="invite-call-id",
    )
    assert msg.startswith("INVITE sip:bob@pbx.example.com SIP/2.0\r\n")
    assert "Call-ID: invite-call-id" in msg
    assert "CSeq: 1 INVITE" in msg
    assert "Content-Type: application/sdp" in msg
