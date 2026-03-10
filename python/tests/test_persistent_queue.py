"""Tests for SQLiteQueue persistent message queue."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from unified_channel.persistent_queue import SQLiteQueue, QueueItem, PersistentQueueMiddleware
from unified_channel.types import OutboundMessage, UnifiedMessage, MessageContent, Identity, ContentType


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_queue.db")


@pytest_asyncio.fixture
async def queue(db_path):
    q = SQLiteQueue(db_path, max_retries=3, retry_delay=60.0)
    yield q
    await q.close()


def _msg(chat_id="123", text="hello") -> OutboundMessage:
    return OutboundMessage(chat_id=chat_id, text=text)


def _unified(chat_id="123", text="hi", channel="telegram") -> UnifiedMessage:
    return UnifiedMessage(
        id="msg-1",
        channel=channel,
        sender=Identity(id="user-1", username="testuser"),
        content=MessageContent(type=ContentType.TEXT, text=text),
        chat_id=chat_id,
    )


@pytest.mark.asyncio
async def test_enqueue_and_pending(queue):
    """Enqueue a message and retrieve it via pending."""
    item_id = await queue.enqueue(_msg(), channel="telegram")
    assert isinstance(item_id, str)
    assert len(item_id) > 0

    items = await queue.pending(limit=10)
    assert len(items) == 1
    assert items[0].id == item_id
    assert items[0].message.chat_id == "123"
    assert items[0].message.text == "hello"
    assert items[0].channel == "telegram"
    assert items[0].status == "processing"


@pytest.mark.asyncio
async def test_ack_removes_from_pending(queue):
    """Acked messages should not appear in pending."""
    item_id = await queue.enqueue(_msg(), channel="telegram")
    items = await queue.pending()
    assert len(items) == 1

    await queue.ack(item_id)

    # Should not appear in pending anymore
    items = await queue.pending()
    assert len(items) == 0


@pytest.mark.asyncio
async def test_nack_increments_retry_count(queue):
    """Nack should increment the retry count."""
    item_id = await queue.enqueue(_msg(), channel="telegram")
    await queue.pending()  # move to processing

    await queue.nack(item_id, error="connection timeout")

    # Check stats - should be back to pending (with future retry time)
    stats = await queue.stats()
    assert stats["pending"] == 1
    assert stats["dead"] == 0


@pytest.mark.asyncio
async def test_nack_moves_to_dead_after_max_retries(queue):
    """After max_retries nacks, the item should be in dead letter queue."""
    item_id = await queue.enqueue(_msg(), channel="telegram")

    for i in range(3):  # max_retries=3
        # Fetch pending - need to bypass the next_retry_at check for test
        db = await queue._connect()
        await db.execute(
            "UPDATE queue_items SET status='pending', next_retry_at=datetime('now','-1 hour') WHERE id=?",
            (item_id,),
        )
        await db.commit()

        items = await queue.pending()
        if items:
            await queue.nack(items[0].id, error=f"fail #{i+1}")

    stats = await queue.stats()
    assert stats["dead"] == 1
    assert stats["pending"] == 0


@pytest.mark.asyncio
async def test_exponential_backoff(queue):
    """Nack should set next_retry_at with exponential backoff."""
    item_id = await queue.enqueue(_msg(), channel="telegram")
    await queue.pending()  # move to processing

    await queue.nack(item_id, error="timeout")

    db = await queue._connect()
    cursor = await db.execute(
        "SELECT retries, next_retry_at FROM queue_items WHERE id = ?", (item_id,)
    )
    row = await cursor.fetchone()
    assert row["retries"] == 1

    # next_retry_at should be set (backoff = 60 * 2^1 = 120s from now)
    next_retry = datetime.fromisoformat(row["next_retry_at"])
    assert next_retry > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_priority_ordering(queue):
    """Higher priority items should be returned first."""
    await queue.enqueue(_msg(text="low"), channel="telegram", priority=1)
    await queue.enqueue(_msg(text="high"), channel="telegram", priority=10)
    await queue.enqueue(_msg(text="medium"), channel="telegram", priority=5)

    items = await queue.pending(limit=10)
    assert len(items) == 3
    assert items[0].message.text == "high"
    assert items[1].message.text == "medium"
    assert items[2].message.text == "low"


@pytest.mark.asyncio
async def test_stats_accuracy(queue):
    """Stats should reflect the actual state of the queue."""
    stats = await queue.stats()
    assert stats["total"] == 0

    id1 = await queue.enqueue(_msg(text="a"), channel="telegram")
    id2 = await queue.enqueue(_msg(text="b"), channel="telegram")
    await queue.enqueue(_msg(text="c"), channel="telegram")

    stats = await queue.stats()
    assert stats["pending"] == 3
    assert stats["total"] == 3

    await queue.pending(limit=2)  # moves 2 to processing
    stats = await queue.stats()
    assert stats["processing"] == 2
    assert stats["pending"] == 1

    await queue.ack(id1)
    stats = await queue.stats()
    assert stats["completed"] == 1


@pytest.mark.asyncio
async def test_purge_completed(queue):
    """Purge should remove old completed items."""
    item_id = await queue.enqueue(_msg(), channel="telegram")
    await queue.pending()
    await queue.ack(item_id)

    # Set updated_at to 48 hours ago
    db = await queue._connect()
    await db.execute(
        "UPDATE queue_items SET updated_at = datetime('now', '-48 hours') WHERE id = ?",
        (item_id,),
    )
    await db.commit()

    removed = await queue.purge_completed(older_than_hours=24)
    assert removed == 1

    stats = await queue.stats()
    assert stats["total"] == 0


@pytest.mark.asyncio
async def test_dead_letters_returns_failed(queue):
    """dead_letters() should return items that exhausted retries."""
    item_id = await queue.enqueue(_msg(), channel="telegram")

    # Exhaust retries
    for i in range(3):
        db = await queue._connect()
        await db.execute(
            "UPDATE queue_items SET status='pending', next_retry_at=datetime('now','-1 hour') WHERE id=?",
            (item_id,),
        )
        await db.commit()
        items = await queue.pending()
        if items:
            await queue.nack(items[0].id, error=f"fail #{i+1}")

    dead = await queue.dead_letters()
    assert len(dead) == 1
    assert dead[0].id == item_id
    assert dead[0].status == "dead"
    assert dead[0].last_error == "fail #3"


@pytest.mark.asyncio
async def test_persistence_across_instances(db_path):
    """Queue data should survive closing and reopening."""
    q1 = SQLiteQueue(db_path)
    item_id = await q1.enqueue(_msg(text="persistent"), channel="telegram")
    await q1.close()

    q2 = SQLiteQueue(db_path)
    items = await q2.pending()
    assert len(items) == 1
    assert items[0].id == item_id
    assert items[0].message.text == "persistent"
    await q2.close()


@pytest.mark.asyncio
async def test_concurrent_enqueue(queue):
    """Multiple items can be enqueued."""
    ids = []
    for i in range(10):
        item_id = await queue.enqueue(_msg(text=f"msg-{i}"), channel="telegram")
        ids.append(item_id)

    assert len(set(ids)) == 10  # all unique

    stats = await queue.stats()
    assert stats["pending"] == 10


@pytest.mark.asyncio
async def test_empty_queue_returns_empty_list(queue):
    """pending() on empty queue returns empty list."""
    items = await queue.pending()
    assert items == []


@pytest.mark.asyncio
async def test_queue_item_dataclass_fields():
    """QueueItem should have all expected fields."""
    now = datetime.now(timezone.utc)
    item = QueueItem(
        id="abc",
        message=_msg(),
        channel="telegram",
        priority=5,
        status="pending",
        retries=2,
        created_at=now,
        next_retry_at=now,
        last_error="some error",
    )
    assert item.id == "abc"
    assert item.message.chat_id == "123"
    assert item.channel == "telegram"
    assert item.priority == 5
    assert item.status == "pending"
    assert item.retries == 2
    assert item.created_at == now
    assert item.next_retry_at == now
    assert item.last_error == "some error"


@pytest.mark.asyncio
async def test_persistent_queue_middleware_integration(db_path):
    """PersistentQueueMiddleware should enqueue replies."""
    q = SQLiteQueue(db_path)
    mw = PersistentQueueMiddleware(q, default_channel="telegram")

    async def handler(msg):
        return "reply text"

    msg = _unified()
    result = await mw.process(msg, handler)

    # Middleware returns None (async delivery)
    assert result is None

    # But message should be in the queue
    items = await q.pending()
    assert len(items) == 1
    assert items[0].message.text == "reply text"
    assert items[0].message.chat_id == "123"
    assert items[0].channel == "telegram"
    await q.close()


@pytest.mark.asyncio
async def test_persistent_queue_middleware_none_reply(db_path):
    """Middleware should not enqueue when handler returns None."""
    q = SQLiteQueue(db_path)
    mw = PersistentQueueMiddleware(q)

    async def handler(msg):
        return None

    result = await mw.process(_unified(), handler)
    assert result is None

    stats = await q.stats()
    assert stats["total"] == 0
    await q.close()


@pytest.mark.asyncio
async def test_enqueue_returns_unique_ids(queue):
    """Each enqueue call should return a unique ID."""
    ids = set()
    for _ in range(20):
        item_id = await queue.enqueue(_msg(), channel="telegram")
        ids.add(item_id)
    assert len(ids) == 20


@pytest.mark.asyncio
async def test_nack_stores_error_message(queue):
    """Nack should store the error message."""
    item_id = await queue.enqueue(_msg(), channel="telegram")
    await queue.pending()

    await queue.nack(item_id, error="Connection refused: port 443")

    db = await queue._connect()
    cursor = await db.execute(
        "SELECT last_error FROM queue_items WHERE id = ?", (item_id,)
    )
    row = await cursor.fetchone()
    assert row["last_error"] == "Connection refused: port 443"


@pytest.mark.asyncio
async def test_middleware_with_outbound_message(db_path):
    """Middleware should handle OutboundMessage replies directly."""
    q = SQLiteQueue(db_path)
    mw = PersistentQueueMiddleware(q)

    outbound = OutboundMessage(chat_id="456", text="direct outbound")

    async def handler(msg):
        return outbound

    result = await mw.process(_unified(), handler)
    assert result is None

    items = await q.pending()
    assert len(items) == 1
    assert items[0].message.chat_id == "456"
    assert items[0].message.text == "direct outbound"
    await q.close()
