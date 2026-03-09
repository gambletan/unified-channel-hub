"""Tests for escalation middleware."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from support.db import Database
from support.models import Agent, AgentStatus, Ticket, TicketMessage, TicketStatus
from support.ai.router import AIRouter
from support.tickets.escalation import EscalationMiddleware


class FakeLLM:
    async def complete(self, messages, system_prompt="", temperature=0.3, max_tokens=1024):
        return "AI reply"

class FakeKB:
    async def search(self, query, top_k=3):
        return []
    def format_context(self, articles):
        return ""


class FakeMessage:
    def __init__(self, text="Help me", ticket=None):
        self.channel = "telegram"
        self.chat_id = "123"
        self.content = type("C", (), {"text": text})()
        self.sender = type("S", (), {"id": "u1", "display_name": "User", "username": "user"})()
        self.metadata = {"ticket": ticket} if ticket else {}


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_no_escalation_normal_message(db):
    router = AIRouter(llm=FakeLLM(), kb=FakeKB())
    mw = EscalationMiddleware(db, router)
    ticket = Ticket(channel="telegram", chat_id="123", customer_id="u1")
    await db.create_ticket(ticket)

    handler = AsyncMock(return_value="Normal reply")
    msg = FakeMessage(text="What is the price?", ticket=ticket)
    result = await mw.process(msg, handler)

    assert result == "Normal reply"
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_escalation_on_keyword(db):
    router = AIRouter(llm=FakeLLM(), kb=FakeKB())
    mw = EscalationMiddleware(db, router)
    ticket = Ticket(channel="telegram", chat_id="123", customer_id="u1")
    await db.create_ticket(ticket)

    handler = AsyncMock()
    msg = FakeMessage(text="I want to talk to a human", ticket=ticket)
    result = await mw.process(msg, handler)

    assert "agent" in result.lower() or "queue" in result.lower()
    handler.assert_not_called()  # Should not reach AI

    # Ticket should be escalated
    updated = await db.get_ticket(ticket.id)
    assert updated.status in (TicketStatus.ESCALATED, TicketStatus.ASSIGNED)


@pytest.mark.asyncio
async def test_escalation_chinese_keyword(db):
    router = AIRouter(llm=FakeLLM(), kb=FakeKB())
    mw = EscalationMiddleware(db, router)
    ticket = Ticket(channel="telegram", chat_id="123", customer_id="u1")
    await db.create_ticket(ticket)

    handler = AsyncMock()
    msg = FakeMessage(text="我要转人工", ticket=ticket)
    result = await mw.process(msg, handler)

    assert "agent" in result.lower() or "queue" in result.lower()


@pytest.mark.asyncio
async def test_escalation_assigns_to_agent(db):
    router = AIRouter(llm=FakeLLM(), kb=FakeKB())
    send_fn = AsyncMock()
    mw = EscalationMiddleware(db, router, send_fn=send_fn)

    # Register an agent
    agent = Agent(id="a1", name="Alice", channel="slack", chat_id="S1", status=AgentStatus.ONLINE)
    await db.upsert_agent(agent)

    ticket = Ticket(channel="telegram", chat_id="123", customer_id="u1")
    await db.create_ticket(ticket)

    handler = AsyncMock()
    msg = FakeMessage(text="speak to someone", ticket=ticket)
    result = await mw.process(msg, handler)

    assert "Alice" in result
    # Agent should be notified
    send_fn.assert_called_once()

    # Ticket assigned
    updated = await db.get_ticket(ticket.id)
    assert updated.status == TicketStatus.ASSIGNED
    assert updated.assigned_agent_id == "a1"


@pytest.mark.asyncio
async def test_escalation_no_agent_available(db):
    router = AIRouter(llm=FakeLLM(), kb=FakeKB())
    mw = EscalationMiddleware(db, router)

    ticket = Ticket(channel="telegram", chat_id="123", customer_id="u1")
    await db.create_ticket(ticket)

    handler = AsyncMock()
    msg = FakeMessage(text="找客服", ticket=ticket)
    result = await mw.process(msg, handler)

    assert "busy" in result.lower() or "patience" in result.lower()
    updated = await db.get_ticket(ticket.id)
    assert updated.status == TicketStatus.ESCALATED


@pytest.mark.asyncio
async def test_assigned_ticket_passes_through(db):
    router = AIRouter(llm=FakeLLM(), kb=FakeKB())
    mw = EscalationMiddleware(db, router)

    ticket = Ticket(channel="telegram", chat_id="123", customer_id="u1",
                    status=TicketStatus.ASSIGNED)
    await db.create_ticket(ticket)

    handler = AsyncMock(return_value="Agent reply")
    msg = FakeMessage(text="anything", ticket=ticket)
    result = await mw.process(msg, handler)

    assert result == "Agent reply"
    handler.assert_called_once()
