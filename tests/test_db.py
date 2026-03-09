"""Tests for the database layer."""

import pytest
import pytest_asyncio

from support.db import Database
from support.models import (
    Agent,
    AgentStatus,
    KBArticle,
    Priority,
    SatisfactionRating,
    Ticket,
    TicketMessage,
    TicketStatus,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_create_and_get_ticket(db):
    t = Ticket(channel="telegram", chat_id="123", customer_id="u1", customer_name="Alice")
    await db.create_ticket(t)
    got = await db.get_ticket(t.id)
    assert got is not None
    assert got.channel == "telegram"
    assert got.customer_name == "Alice"
    assert got.status == TicketStatus.OPEN


@pytest.mark.asyncio
async def test_find_ticket_by_chat(db):
    t = Ticket(channel="discord", chat_id="456", customer_id="u2")
    await db.create_ticket(t)
    found = await db.find_ticket_by_chat("discord", "456")
    assert found is not None
    assert found.id == t.id

    # No match
    assert await db.find_ticket_by_chat("slack", "999") is None


@pytest.mark.asyncio
async def test_update_ticket_status(db):
    t = Ticket(channel="telegram", chat_id="789", customer_id="u3")
    await db.create_ticket(t)
    await db.update_ticket_status(t.id, TicketStatus.RESOLVED)
    got = await db.get_ticket(t.id)
    assert got.status == TicketStatus.RESOLVED
    assert got.resolved_at is not None


@pytest.mark.asyncio
async def test_list_tickets_with_filter(db):
    for i in range(5):
        t = Ticket(channel="telegram", chat_id=str(i), customer_id=f"u{i}")
        await db.create_ticket(t)
    await db.update_ticket_status((await db.list_tickets())[0].id, TicketStatus.RESOLVED)

    all_tickets = await db.list_tickets()
    assert len(all_tickets) == 5

    open_tickets = await db.list_tickets(status=TicketStatus.OPEN)
    assert len(open_tickets) == 4

    resolved = await db.list_tickets(status=TicketStatus.RESOLVED)
    assert len(resolved) == 1


@pytest.mark.asyncio
async def test_count_tickets(db):
    for i in range(3):
        await db.create_ticket(Ticket(channel="t", chat_id=str(i), customer_id=f"u{i}"))
    assert await db.count_tickets() == 3
    assert await db.count_tickets(TicketStatus.OPEN) == 3


@pytest.mark.asyncio
async def test_add_and_get_messages(db):
    t = Ticket(channel="telegram", chat_id="c1", customer_id="u1")
    await db.create_ticket(t)

    await db.add_message(TicketMessage(ticket_id=t.id, role="customer", content="Hello"))
    await db.add_message(TicketMessage(ticket_id=t.id, role="ai", content="Hi! How can I help?"))

    msgs = await db.get_messages(t.id)
    assert len(msgs) == 2
    assert msgs[0].role == "customer"
    assert msgs[1].role == "ai"


@pytest.mark.asyncio
async def test_agent_crud(db):
    agent = Agent(id="a1", name="Bob", channel="slack", chat_id="S123", status=AgentStatus.ONLINE)
    await db.upsert_agent(agent)

    agents = await db.list_agents()
    assert len(agents) == 1
    assert agents[0].name == "Bob"

    # Get available
    avail = await db.get_available_agent()
    assert avail is not None
    assert avail.id == "a1"

    # Find by chat
    found = await db.find_agent_by_chat("slack", "S123")
    assert found is not None
    assert found.id == "a1"


@pytest.mark.asyncio
async def test_agent_load(db):
    agent = Agent(id="a2", name="Carol", status=AgentStatus.ONLINE, max_concurrent=2)
    await db.upsert_agent(agent)

    await db.update_agent_load("a2", 1)
    await db.update_agent_load("a2", 1)

    # Now at max, should not be available
    avail = await db.get_available_agent()
    assert avail is None

    # Decrease load
    await db.update_agent_load("a2", -1)
    avail = await db.get_available_agent()
    assert avail is not None


@pytest.mark.asyncio
async def test_kb_index_and_search(db):
    article = KBArticle(title="Pricing", content="Our service costs $10/month", category="billing")
    await db.index_article(article)

    results = await db.search_kb("pricing cost")
    assert len(results) >= 1
    assert results[0].title == "Pricing"


@pytest.mark.asyncio
async def test_kb_clear(db):
    await db.index_article(KBArticle(title="Test", content="test content"))
    await db.clear_kb()
    results = await db.search_kb("test")
    assert len(results) == 0


@pytest.mark.asyncio
async def test_analytics_event(db):
    await db.log_event("ticket_created", ticket_id="t1")
    await db.log_event("first_response", ticket_id="t1", value_ms=1500)
    # No assert needed — just verify no errors


@pytest.mark.asyncio
async def test_satisfaction_rating(db):
    t = Ticket(channel="t", chat_id="c", customer_id="u")
    await db.create_ticket(t)
    await db.add_rating(SatisfactionRating(ticket_id=t.id, rating=5, comment="Great!"))
