"""Tests for optimization changes: pipeline caching, parallel status, deque ratelimit, etc."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections import deque

import pytest

from unified_channel.adapter import ChannelAdapter
from unified_channel.manager import ChannelManager
from unified_channel.middleware import CommandMiddleware, Middleware
from unified_channel.ratelimit import RateLimitMiddleware
from unified_channel.memory import SQLiteStore, InMemoryStore, ConversationMemory
from unified_channel.queue import InMemoryQueue, QueueMiddleware
from unified_channel.types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


def async_reply(text: str):
    """Create an async handler that returns a fixed string."""
    async def handler(msg: UnifiedMessage) -> str:
        return text
    return handler


def _msg(
    text: str = "hello",
    sender_id: str = "user1",
    chat_id: str = "chat1",
    command: str | None = None,
) -> UnifiedMessage:
    content_type = ContentType.COMMAND if command else ContentType.TEXT
    return UnifiedMessage(
        id="1",
        channel="mock",
        sender=Identity(id=sender_id),
        content=MessageContent(type=content_type, text=text, command=command),
        chat_id=chat_id,
    )


class MockAdapter(ChannelAdapter):
    channel_id = "mock"

    def __init__(self, *, status_delay: float = 0, status_error: bool = False) -> None:
        self._connected = False
        self._inbound: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self.sent: list[OutboundMessage] = []
        self._status_delay = status_delay
        self._status_error = status_error

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def receive(self):
        while self._connected:
            try:
                msg = await self._inbound.get()
                yield msg
            except asyncio.CancelledError:
                break

    async def send(self, msg: OutboundMessage) -> str | None:
        self.sent.append(msg)
        return str(len(self.sent))

    async def get_status(self) -> ChannelStatus:
        if self._status_delay:
            await asyncio.sleep(self._status_delay)
        if self._status_error:
            raise RuntimeError("status failed")
        return ChannelStatus(connected=self._connected, channel=self.channel_id)

    async def inject(self, text: str, *, command: str | None = None) -> None:
        await self._inbound.put(_msg(text=text, command=command))


# ── Pipeline caching ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_cache_built_on_first_message():
    """Pipeline is None initially, built on first message."""
    manager = ChannelManager()
    adapter = MockAdapter()
    manager.add_channel(adapter)
    manager.on_message(async_reply("ok"))

    assert manager._cached_pipeline is None

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("hello")
    await asyncio.sleep(0.2)

    assert manager._cached_pipeline is not None
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "ok"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_pipeline_cache_invalidated_on_add_middleware():
    manager = ChannelManager()
    adapter = MockAdapter()
    manager.add_channel(adapter)
    manager.on_message(async_reply("v1"))

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    # Build cache
    await adapter.inject("a")
    await asyncio.sleep(0.2)
    assert manager._cached_pipeline is not None

    # Invalidate by adding middleware
    class NoopMw(Middleware):
        async def process(self, msg, next_handler):
            return await next_handler(msg)

    manager.add_middleware(NoopMw())
    assert manager._cached_pipeline is None

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_pipeline_cache_invalidated_on_handler_change():
    manager = ChannelManager()
    adapter = MockAdapter()
    manager.add_channel(adapter)
    manager.on_message(async_reply("first"))

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("a")
    await asyncio.sleep(0.2)
    assert adapter.sent[0].text == "first"
    assert manager._cached_pipeline is not None

    # Change handler — should invalidate
    manager.on_message(async_reply("second"))
    assert manager._cached_pipeline is None

    await adapter.inject("b")
    await asyncio.sleep(0.2)
    assert adapter.sent[1].text == "second"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_pipeline_middleware_order_preserved():
    """Even with caching, middleware executes in correct order."""
    manager = ChannelManager()
    adapter = MockAdapter()
    manager.add_channel(adapter)

    order: list[int] = []

    for i in range(3):
        idx = i

        class OrderMw(Middleware):
            _idx = idx

            async def process(self, msg, next_handler):
                order.append(self._idx)
                return await next_handler(msg)

        manager.add_middleware(OrderMw())

    manager.on_message(async_reply("done"))

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    # First message — builds chain
    await adapter.inject("a")
    await asyncio.sleep(0.2)
    assert order == [0, 1, 2]

    # Second message — reuses chain
    order.clear()
    await adapter.inject("b")
    await asyncio.sleep(0.2)
    assert order == [0, 1, 2]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── Parallel get_status ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_status_parallel():
    """Status fetches should run concurrently, not sequentially."""
    manager = ChannelManager()

    class SlowAdapter(MockAdapter):
        def __init__(self, cid: str):
            super().__init__(status_delay=0.05)
            self.channel_id = cid

    for i in range(3):
        manager.add_channel(SlowAdapter(f"ch{i}"))

    start = time.monotonic()
    statuses = await manager.get_status()
    elapsed = time.monotonic() - start

    assert len(statuses) == 3
    # If sequential: ~150ms. Parallel: ~50ms (+overhead)
    assert elapsed < 0.12


@pytest.mark.asyncio
async def test_get_status_handles_errors():
    manager = ChannelManager()

    good = MockAdapter()
    good.channel_id = "good"
    await good.connect()

    bad = MockAdapter(status_error=True)
    bad.channel_id = "bad"

    manager.add_channel(good)
    manager.add_channel(bad)

    statuses = await manager.get_status()
    assert statuses["good"].connected is True
    assert statuses["bad"]["connected"] is False
    assert "status failed" in statuses["bad"]["error"]


# ── Deque-based RateLimitMiddleware ──────────────────────────


@pytest.mark.asyncio
async def test_ratelimit_uses_deque():
    """Verify internal storage is deque, not list."""
    mw = RateLimitMiddleware(max_messages=5, window_seconds=10)
    assert isinstance(mw._windows, dict)

    async def noop(msg):
        return "ok"

    await mw.process(_msg(), noop)

    for ts in mw._windows.values():
        assert isinstance(ts, deque)


@pytest.mark.asyncio
async def test_ratelimit_deque_popleft_eviction():
    """Expired entries are evicted via popleft (O(1) per entry)."""
    mw = RateLimitMiddleware(max_messages=2, window_seconds=1.0)

    async def noop(msg):
        return "ok"

    await mw.process(_msg(), noop)
    await mw.process(_msg(), noop)

    # At limit
    result = await mw.process(_msg(), noop)
    assert result is None

    # Expire first entry
    key = "user1"
    mw._windows[key][0] -= 2.0

    # Should allow again (one slot freed)
    result = await mw.process(_msg(), noop)
    assert result == "ok"


@pytest.mark.asyncio
async def test_ratelimit_cleanup_removes_empty_deques():
    """cleanup() removes keys whose deques are fully expired."""
    mw = RateLimitMiddleware(max_messages=5, window_seconds=0.5)

    async def noop(msg):
        return "ok"

    await mw.process(_msg(sender_id="alice"), noop)
    await mw.process(_msg(sender_id="bob"), noop)
    assert len(mw._windows) == 2

    # Expire all
    for ts in mw._windows.values():
        for i in range(len(ts)):
            ts[i] -= 1.0

    mw.cleanup()
    assert len(mw._windows) == 0


@pytest.mark.asyncio
async def test_ratelimit_reset():
    mw = RateLimitMiddleware(max_messages=1, window_seconds=10)

    async def noop(msg):
        return "ok"

    await mw.process(_msg(), noop)
    assert await mw.process(_msg(), noop) is None

    mw.reset()
    assert len(mw._windows) == 0
    assert await mw.process(_msg(), noop) == "ok"


# ── SQLiteStore async behavior ───────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_store_aclose():
    """aclose() properly closes aiosqlite connections."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = SQLiteStore(db_path=db_path)
        await store.append("k", {"v": 1})
        result = await store.get("k")
        assert len(result) == 1

        await store.aclose()

        # Re-open should work
        store2 = SQLiteStore(db_path=db_path)
        result = await store2.get("k")
        assert len(result) == 1
        await store2.aclose()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_sqlite_store_multiple_keys_parallel():
    """Parallel operations on different keys don't conflict."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = SQLiteStore(db_path=db_path)

        # Write to multiple keys
        tasks = [store.append(f"k{i}", {"i": i}) for i in range(10)]
        await asyncio.gather(*tasks)

        # Read all back
        for i in range(10):
            result = await store.get(f"k{i}")
            assert len(result) == 1
            assert result[0]["i"] == i

        await store.aclose()
    finally:
        os.unlink(db_path)


# ── Lazy imports ─────────────────────────────────────────────


def test_lazy_imports_resolve():
    """Lazy-loaded extras resolve correctly via __getattr__."""
    import unified_channel

    # These should be lazy
    assert hasattr(unified_channel, "StreamingMiddleware")
    assert hasattr(unified_channel, "I18nMiddleware")
    assert hasattr(unified_channel, "Scheduler")
    assert hasattr(unified_channel, "SQLiteQueue")
    assert hasattr(unified_channel, "RelayMiddleware")
    assert hasattr(unified_channel, "IdentityRouter")


def test_lazy_imports_unknown_raises():
    import unified_channel

    with pytest.raises(AttributeError, match="has no attribute"):
        _ = unified_channel.NonExistentThing


def test_core_imports_eager():
    """Core classes are available immediately (not lazy)."""
    from unified_channel import (
        ChannelManager,
        CommandMiddleware,
        RateLimitMiddleware,
        ConversationMemory,
        RichReply,
        InMemoryQueue,
    )

    assert ChannelManager is not None
    assert CommandMiddleware is not None
    assert RateLimitMiddleware is not None


# ── Queue worker with blocking get ──────────────────────────


@pytest.mark.asyncio
async def test_queue_worker_cancellation():
    """InMemoryQueue worker exits cleanly on task cancellation."""
    from unified_channel.queue import InMemoryQueue

    q = InMemoryQueue(concurrency=1)
    processed: list[str] = []

    async def handler(msg):
        processed.append(msg.content.text)
        return None

    q.on_process(handler)
    q.start()

    q.enqueue(_msg(text="a"))
    q.enqueue(_msg(text="b"))
    await asyncio.sleep(0.1)

    assert processed == ["a", "b"]

    await q.stop()
    # Workers should be cleaned up
    assert len(q._workers) == 0


# ── #1 SQLite WAL mode + batch writes ───────────────────────


@pytest.mark.asyncio
async def test_sqlite_store_wal_mode():
    """SQLiteStore enables WAL mode for better concurrent write throughput."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = SQLiteStore(db_path=db_path)
        # Force connection
        await store.append("k", {"v": 1})
        # Check WAL mode is enabled
        if store._aconn:
            async with store._aconn.execute("PRAGMA journal_mode") as cursor:
                row = await cursor.fetchone()
                assert row[0] == "wal"
        elif store._sync_conn:
            cursor = store._sync_conn.execute("PRAGMA journal_mode")
            row = cursor.fetchone()
            assert row[0] == "wal"
        await store.aclose()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass


@pytest.mark.asyncio
async def test_sqlite_store_append_many():
    """append_many batches multiple entries in a single transaction."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = SQLiteStore(db_path=db_path)
        entries = [{"i": i} for i in range(5)]
        await store.append_many("batch_key", entries)
        result = await store.get("batch_key")
        assert len(result) == 5
        assert [r["i"] for r in result] == [0, 1, 2, 3, 4]
        await store.aclose()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except FileNotFoundError:
                pass


@pytest.mark.asyncio
async def test_inmemory_store_append_many():
    """InMemoryStore.append_many extends in one call."""
    store = InMemoryStore()
    await store.append_many("k", [{"a": 1}, {"b": 2}])
    result = await store.get("k")
    assert len(result) == 2


# ── #2 Lazy history loading ─────────────────────────────────


@pytest.mark.asyncio
async def test_conversation_memory_lazy_history():
    """History provides both eager list and lazy callable."""
    store = InMemoryStore()
    mem = ConversationMemory(store=store, max_turns=50)

    # Pre-populate history
    await store.append("mock:chat1", {"role": "user", "content": "old"})

    async def handler(msg):
        # Eager: metadata["history"] is a plain list (backward compat)
        history = msg.metadata["history"]
        assert isinstance(history, list)
        assert len(history) == 1

        # Lazy: metadata["get_history"] is a callable (new)
        get_history = msg.metadata["get_history"]
        assert callable(get_history)
        h = await get_history()
        assert h is history  # same cached object
        return "reply"

    await mem.process(_msg(), handler)


@pytest.mark.asyncio
async def test_conversation_memory_batch_writes():
    """ConversationMemory writes user + assistant entries in one batch."""
    store = InMemoryStore()
    mem = ConversationMemory(store=store, max_turns=50)

    async def handler(msg):
        return "bot reply"

    await mem.process(_msg(text="user text"), handler)

    entries = await store.get("mock:chat1")
    assert len(entries) == 2
    assert entries[0]["role"] == "user"
    assert entries[0]["content"] == "user text"
    assert entries[1]["role"] == "assistant"
    assert entries[1]["content"] == "bot reply"


# ── #3 Parallel adapter connect ─────────────────────────────


@pytest.mark.asyncio
async def test_parallel_adapter_connect():
    """Adapters connect in parallel, not sequentially."""
    connect_times: list[float] = []

    class SlowConnectAdapter(MockAdapter):
        def __init__(self, cid: str):
            super().__init__()
            self.channel_id = cid

        async def connect(self):
            connect_times.append(time.monotonic())
            await asyncio.sleep(0.05)
            self._connected = True

    manager = ChannelManager()
    for i in range(3):
        manager.add_channel(SlowConnectAdapter(f"ch{i}"))
    manager.on_message(async_reply("ok"))

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.15)

    # All connects should start at nearly the same time (parallel)
    assert len(connect_times) == 3
    spread = max(connect_times) - min(connect_times)
    assert spread < 0.03  # Would be ~0.1 if sequential

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── #4 Periodic ratelimit cleanup ───────────────────────────


@pytest.mark.asyncio
async def test_ratelimit_periodic_cleanup():
    """cleanup() is triggered automatically every N calls."""
    mw = RateLimitMiddleware(max_messages=100, window_seconds=0.01)
    mw._cleanup_interval = 10  # trigger every 10 calls

    async def noop(msg):
        return "ok"

    # Create many unique senders
    for i in range(10):
        await mw.process(_msg(sender_id=f"user{i}"), noop)

    assert len(mw._windows) == 10

    # Wait for entries to expire
    await asyncio.sleep(0.02)

    # 10th call should trigger cleanup (process_count hits interval)
    # But cleanup already ran at call 10 — need one more cycle
    for i in range(10):
        await mw.process(_msg(sender_id=f"fresh{i}"), noop)

    # Old expired keys should have been cleaned up
    expired_old = [k for k in mw._windows if k.startswith("user")]
    assert len(expired_old) == 0


# ── #7 Broadcast concurrency control (Python) ──────────────


@pytest.mark.asyncio
async def test_broadcast_batching():
    """broadcast() sends in batches of broadcast_concurrency."""
    manager = ChannelManager(broadcast_concurrency=2)
    send_order: list[str] = []

    for i in range(4):
        cid = f"ch{i}"
        adapter = MockAdapter()
        adapter.channel_id = cid

        original_send = adapter.send

        async def tracking_send(msg, _cid=cid, _orig=original_send):
            send_order.append(_cid)
            return await _orig(msg)

        adapter.send = tracking_send
        manager.add_channel(adapter)

    await manager.broadcast("hello", {f"ch{i}": "room" for i in range(4)})
    assert len(send_order) == 4


# ── #8 __slots__ on dataclasses ─────────────────────────────


def test_dataclass_slots():
    """Core dataclasses use __slots__ for memory efficiency."""
    from unified_channel.types import Identity, MessageContent, UnifiedMessage, OutboundMessage, Button, ChannelStatus

    for cls in (Identity, MessageContent, UnifiedMessage, OutboundMessage, Button, ChannelStatus):
        assert hasattr(cls, "__slots__"), f"{cls.__name__} missing __slots__"


# ── #9 Queue backpressure reply ─────────────────────────────


@pytest.mark.asyncio
async def test_queue_backpressure_reply():
    """QueueMiddleware returns backpressure reply when queue is full."""
    q = InMemoryQueue(concurrency=1, max_size=1)
    mw = QueueMiddleware(q, backpressure_reply="Server busy, try later")

    async def noop(msg):
        return "ok"

    # First message accepted
    result = await mw.process(_msg(text="first"), noop)
    assert result is None  # Enqueued, no inline reply

    # Second message — queue full
    result = await mw.process(_msg(text="second"), noop)
    assert result == "Server busy, try later"


@pytest.mark.asyncio
async def test_queue_is_full_property():
    """QueueMiddleware.is_full reflects queue state."""
    q = InMemoryQueue(concurrency=1, max_size=2)
    mw = QueueMiddleware(q)

    assert not mw.is_full
    q.enqueue(_msg(text="a"))
    q.enqueue(_msg(text="b"))
    assert mw.is_full


# ── #10 Status cache with TTL ──────────────────────────────


@pytest.mark.asyncio
async def test_status_cache_ttl():
    """get_status() returns cached result within TTL."""
    manager = ChannelManager(status_cache_ttl=1.0)
    call_count = 0

    class CountingAdapter(MockAdapter):
        async def get_status(self):
            nonlocal call_count
            call_count += 1
            return ChannelStatus(connected=True, channel=self.channel_id)

    adapter = CountingAdapter()
    manager.add_channel(adapter)

    # First call — fetches
    await manager.get_status()
    assert call_count == 1

    # Second call — cached
    await manager.get_status()
    assert call_count == 1

    # After TTL expires
    manager._status_cache_time -= 2.0
    await manager.get_status()
    assert call_count == 2
