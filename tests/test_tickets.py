"""Tests for ticket middleware."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from support.db import Database
from support.models import Ticket, TicketMessage, TicketStatus
from support.tickets.manager import TicketMiddleware


class FakeMessage:
    def __init__(self, channel="telegram", chat_id="123", sender_id="u1", text="Hello"):
        self.channel = channel
        self.chat_id = chat_id
        self.content = type("C", (), {"text": text, "type": "text"})()
        self.sender = type("S", (), {
            "id": sender_id, "display_name": "Test User", "username": "testuser"
        })()
        self.metadata = {}
        self.id = "msg1"
        self.timestamp = datetime.now(timezone.utc)
        self.thread_id = None
        self.raw = None


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_ticket_created_on_first_message(db):
    mw = TicketMiddleware(db)
    msg = FakeMessage()
    handler = AsyncMock(return_value="Got it!")

    result = await mw.process(msg, handler)

    assert result == "Got it!"
    handler.assert_called_once()

    # Ticket should be created
    ticket = await db.find_ticket_by_chat("telegram", "123")
    assert ticket is not None
    assert ticket.status == TicketStatus.OPEN
    assert ticket.customer_name == "Test User"


@pytest.mark.asyncio
async def test_same_chat_reuses_ticket(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="OK")

    await mw.process(FakeMessage(text="First message"), handler)
    await mw.process(FakeMessage(text="Second message"), handler)

    tickets = await db.list_tickets()
    assert len(tickets) == 1  # Same ticket reused

    msgs = await db.get_messages(tickets[0].id)
    assert len(msgs) == 4  # 2 customer + 2 AI replies


@pytest.mark.asyncio
async def test_different_chats_create_different_tickets(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="OK")

    await mw.process(FakeMessage(chat_id="a"), handler)
    await mw.process(FakeMessage(chat_id="b"), handler)

    tickets = await db.list_tickets()
    assert len(tickets) == 2


@pytest.mark.asyncio
async def test_messages_stored(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="Hello! How can I help?")

    await mw.process(FakeMessage(text="I need help"), handler)

    tickets = await db.list_tickets()
    msgs = await db.get_messages(tickets[0].id)
    assert len(msgs) == 2
    assert msgs[0].role == "customer"
    assert msgs[0].content == "I need help"
    assert msgs[1].role == "ai"
    assert msgs[1].content == "Hello! How can I help?"


@pytest.mark.asyncio
async def test_subject_extraction(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="OK")

    await mw.process(FakeMessage(text="My order #12345 is missing"), handler)
    ticket = await db.find_ticket_by_chat("telegram", "123")
    assert ticket.subject == "My order #12345 is missing"


@pytest.mark.asyncio
async def test_long_subject_truncated(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="OK")

    long_text = "A" * 100
    await mw.process(FakeMessage(text=long_text), handler)
    ticket = await db.find_ticket_by_chat("telegram", "123")
    assert len(ticket.subject) <= 53  # 50 + "..."
