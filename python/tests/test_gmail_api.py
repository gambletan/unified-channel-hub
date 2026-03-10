"""Tests for GmailAPIAdapter."""

from __future__ import annotations

import pytest

from unified_channel.adapters.gmail_api import GmailAPIAdapter


def test_gmail_channel_id():
    adapter = GmailAPIAdapter("/fake/creds.json")
    assert adapter.channel_id == "gmail"


def test_gmail_defaults():
    adapter = GmailAPIAdapter("/fake/creds.json")
    assert adapter._token_path == "gmail_token.json"
    assert adapter._poll_interval == 30.0
    assert adapter._label_ids == ["INBOX"]
    assert adapter._connected is False
    assert adapter._service is None


def test_gmail_custom_options():
    adapter = GmailAPIAdapter(
        "/fake/creds.json",
        token_path="/custom/token.json",
        poll_interval=60.0,
        label_ids=["INBOX", "IMPORTANT"],
    )
    assert adapter._token_path == "/custom/token.json"
    assert adapter._poll_interval == 60.0
    assert adapter._label_ids == ["INBOX", "IMPORTANT"]


def test_gmail_default_scopes():
    adapter = GmailAPIAdapter("/fake/creds.json")
    assert "https://www.googleapis.com/auth/gmail.readonly" in adapter._scopes
    assert "https://www.googleapis.com/auth/gmail.send" in adapter._scopes
    assert "https://www.googleapis.com/auth/gmail.modify" in adapter._scopes


def test_gmail_custom_scopes():
    custom = ["https://www.googleapis.com/auth/gmail.readonly"]
    adapter = GmailAPIAdapter("/fake/creds.json", scopes=custom)
    assert adapter._scopes == custom


@pytest.mark.asyncio
async def test_gmail_status_disconnected():
    adapter = GmailAPIAdapter("/fake/creds.json")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "gmail"
    assert status.account_id == "unknown"
    assert status.last_activity is None


@pytest.mark.asyncio
async def test_gmail_send_not_connected():
    from unified_channel.types import OutboundMessage
    adapter = GmailAPIAdapter("/fake/creds.json")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send(OutboundMessage(chat_id="test@example.com", text="Hello"))


def test_gmail_parse_sender_with_name():
    name, email = GmailAPIAdapter._parse_sender("John Doe <john@example.com>")
    assert name == "John Doe"
    assert email == "john@example.com"


def test_gmail_parse_sender_bare_email():
    name, email = GmailAPIAdapter._parse_sender("john@example.com")
    assert name == ""
    assert email == "john@example.com"


def test_gmail_parse_sender_quoted_name():
    name, email = GmailAPIAdapter._parse_sender('"Jane Doe" <jane@example.com>')
    assert name == "Jane Doe"
    assert email == "jane@example.com"


def test_gmail_extract_body_plain():
    payload = {
        "mimeType": "text/plain",
        "body": {"data": "SGVsbG8gV29ybGQ="},  # "Hello World"
    }
    assert GmailAPIAdapter._extract_body(payload) == "Hello World"


def test_gmail_extract_body_multipart():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}},  # "Hello"
            {"mimeType": "text/html", "body": {"data": "PGI-SGVsbG88L2I-"}},
        ],
    }
    assert GmailAPIAdapter._extract_body(payload) == "Hello"


def test_gmail_extract_body_empty():
    payload = {"mimeType": "multipart/mixed", "parts": []}
    assert GmailAPIAdapter._extract_body(payload) == ""


def test_gmail_has_attachments():
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": "dGVzdA=="}},
            {"filename": "report.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "abc"}},
        ],
    }
    assert GmailAPIAdapter._has_attachments(payload) is True


def test_gmail_no_attachments():
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": "dGVzdA=="}},
        ],
    }
    assert GmailAPIAdapter._has_attachments(payload) is False
