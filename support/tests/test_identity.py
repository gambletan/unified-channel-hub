"""Tests for identity binding middleware."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from support.db import Database
from support.tickets.identity import IdentityMiddleware


class FakeMessage:
    def __init__(self, text="Hello", channel="telegram", chat_id="tg_999"):
        self.channel = channel
        self.chat_id = chat_id
        self.content = type("C", (), {"text": text})()
        self.sender = type("S", (), {
            "id": "sender1", "display_name": "TestUser", "username": "testuser"
        })()
        self.metadata = {}


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_bind_via_start_command(db):
    """Telegram /start uid_12345 should bind the user."""
    mw = IdentityMiddleware(db)
    handler = AsyncMock(return_value="OK")

    msg = FakeMessage(text="/start uid_12345")
    result = await mw.process(msg, handler)

    # Should return welcome message, NOT call handler
    assert "linked" in result.lower() or "account" in result.lower()
    handler.assert_not_called()

    # Binding should exist
    binding = await db.get_binding_by_chat("telegram", "tg_999")
    assert binding is not None
    assert binding.platform_user_id == "12345"


@pytest.mark.asyncio
async def test_bind_via_uid_prefix(db):
    """WhatsApp first message uid_ABC should bind."""
    mw = IdentityMiddleware(db)
    handler = AsyncMock()

    msg = FakeMessage(text="uid_ABC123", channel="whatsapp", chat_id="wa_555")
    result = await mw.process(msg, handler)

    binding = await db.get_binding_by_chat("whatsapp", "wa_555")
    assert binding is not None
    assert binding.platform_user_id == "ABC123"


@pytest.mark.asyncio
async def test_bind_via_explicit_bind_command(db):
    mw = IdentityMiddleware(db)
    handler = AsyncMock()

    msg = FakeMessage(text="bind USER999", channel="discord", chat_id="dc_111")
    await mw.process(msg, handler)

    binding = await db.get_binding_by_chat("discord", "dc_111")
    assert binding is not None
    assert binding.platform_user_id == "USER999"


@pytest.mark.asyncio
async def test_normal_message_passes_through(db):
    """Regular messages should pass through to next handler."""
    mw = IdentityMiddleware(db)
    handler = AsyncMock(return_value="Normal reply")

    msg = FakeMessage(text="How much does it cost?")
    result = await mw.process(msg, handler)

    assert result == "Normal reply"
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_bound_user_gets_metadata_injected(db):
    """After binding, subsequent messages get platform_user_id in metadata."""
    mw = IdentityMiddleware(db)

    # First: bind
    handler = AsyncMock()
    await mw.process(FakeMessage(text="/start uid_PLAT42"), handler)

    # Second: normal message from same chat
    handler2 = AsyncMock(return_value="OK")
    msg2 = FakeMessage(text="I need help")
    await mw.process(msg2, handler2)

    handler2.assert_called_once()
    assert msg2.metadata.get("platform_user_id") == "PLAT42"


@pytest.mark.asyncio
async def test_rebind_updates_user(db):
    """Rebinding updates the platform user ID."""
    mw = IdentityMiddleware(db)
    handler = AsyncMock()

    await mw.process(FakeMessage(text="/start uid_OLD"), handler)
    await mw.process(FakeMessage(text="/start uid_NEW"), handler)

    binding = await db.get_binding_by_chat("telegram", "tg_999")
    assert binding.platform_user_id == "NEW"


@pytest.mark.asyncio
async def test_get_bindings_by_user(db):
    """Can look up all channels for a platform user."""
    mw = IdentityMiddleware(db)
    handler = AsyncMock()

    await mw.process(FakeMessage(text="/start uid_MULTI", channel="telegram", chat_id="tg1"), handler)
    await mw.process(FakeMessage(text="uid_MULTI", channel="whatsapp", chat_id="wa1"), handler)

    bindings = await db.get_bindings_by_user("MULTI")
    assert len(bindings) == 2
    channels = {b.channel for b in bindings}
    assert channels == {"telegram", "whatsapp"}


@pytest.mark.asyncio
async def test_custom_welcome_message(db):
    mw = IdentityMiddleware(db, welcome_msg="Yay! Linked!")
    handler = AsyncMock()

    msg = FakeMessage(text="/start uid_X")
    result = await mw.process(msg, handler)
    assert result == "Yay! Linked!"
