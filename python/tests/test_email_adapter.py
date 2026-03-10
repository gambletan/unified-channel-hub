"""Tests for EmailAdapter — mock IMAP/SMTP."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import email.mime.text

import pytest

from unified_channel.adapters.email_imap import EmailAdapter, _PRESETS


def test_gmail_preset():
    """Gmail preset should set correct IMAP/SMTP hosts."""
    adapter = EmailAdapter("test@gmail.com", "pass", preset="gmail")
    assert adapter._imap_host == "imap.gmail.com"
    assert adapter._smtp_host == "smtp.gmail.com"
    assert adapter._smtp_port == 587


def test_outlook_preset():
    """Outlook preset should set correct hosts."""
    adapter = EmailAdapter("test@outlook.com", "pass", preset="outlook")
    assert adapter._imap_host == "outlook.office365.com"
    assert adapter._smtp_host == "smtp.office365.com"


def test_custom_host():
    """Custom hosts override presets."""
    adapter = EmailAdapter(
        "test@custom.com", "pass",
        imap_host="mail.custom.com", smtp_host="smtp.custom.com"
    )
    assert adapter._imap_host == "mail.custom.com"
    assert adapter._smtp_host == "smtp.custom.com"


def test_default_mailbox():
    """Default mailbox is INBOX."""
    adapter = EmailAdapter("test@test.com", "pass", imap_host="localhost", smtp_host="localhost")
    assert adapter._mailbox == "INBOX"


def test_channel_id():
    """Channel ID should be 'email'."""
    adapter = EmailAdapter("test@test.com", "pass", imap_host="localhost", smtp_host="localhost")
    assert adapter.channel_id == "email"


@pytest.mark.asyncio
async def test_get_status_disconnected():
    """Status should show disconnected before connect."""
    adapter = EmailAdapter("test@test.com", "pass", imap_host="localhost", smtp_host="localhost")
    status = await adapter.get_status()
    assert status.connected is False
    assert status.channel == "email"
    assert status.account_id == "test@test.com"


def test_extract_body_plain():
    """Extract body from plain text email."""
    from email.mime.text import MIMEText
    msg = MIMEText("Hello world")
    body = EmailAdapter._extract_body(msg)
    assert body == "Hello world"


def test_extract_body_multipart():
    """Extract plain text from multipart email."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart()
    msg.attach(MIMEText("Plain text part"))
    msg.attach(MIMEText("<html>HTML part</html>", "html"))

    body = EmailAdapter._extract_body(msg)
    assert body == "Plain text part"


def test_presets_exist():
    """Gmail and Outlook presets should be defined."""
    assert "gmail" in _PRESETS
    assert "outlook" in _PRESETS
    for preset in _PRESETS.values():
        assert "imap" in preset
        assert "smtp" in preset
        assert "smtp_port" in preset
