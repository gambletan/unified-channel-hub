"""Tests for ConversationMemory middleware and MemoryStore backends."""

from __future__ import annotations

import os
import tempfile

import pytest

from unified_channel.memory import ConversationMemory, InMemoryStore, SQLiteStore
from unified_channel.types import (
    ContentType,
    Identity,
    MessageContent,
    UnifiedMessage,
)


def _msg(
    text: str = "hello",
    sender_id: str = "user1",
    channel: str = "test",
    chat_id: str = "chat1",
) -> UnifiedMessage:
    return UnifiedMessage(
        id="1",
        channel=channel,
        sender=Identity(id=sender_id),
        content=MessageContent(type=ContentType.TEXT, text=text),
        chat_id=chat_id,
    )


# ── InMemoryStore ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inmemory_store_empty():
    store = InMemoryStore()
    result = await store.get("nonexistent")
    assert result == []


@pytest.mark.asyncio
async def test_inmemory_store_append_and_get():
    store = InMemoryStore()
    await store.append("k1", {"role": "user", "content": "hi"})
    await store.append("k1", {"role": "assistant", "content": "hello"})
    result = await store.get("k1")
    assert len(result) == 2
    assert result[0]["content"] == "hi"
    assert result[1]["content"] == "hello"


@pytest.mark.asyncio
async def test_inmemory_store_trim():
    store = InMemoryStore()
    for i in range(10):
        await store.append("k", {"i": i})
    await store.trim("k", 3)
    result = await store.get("k")
    assert len(result) == 3
    assert result[0]["i"] == 7


@pytest.mark.asyncio
async def test_inmemory_store_clear():
    store = InMemoryStore()
    await store.append("k", {"x": 1})
    await store.clear("k")
    result = await store.get("k")
    assert result == []


@pytest.mark.asyncio
async def test_inmemory_store_isolation():
    """Different keys are independent."""
    store = InMemoryStore()
    await store.append("a", {"v": 1})
    await store.append("b", {"v": 2})
    assert len(await store.get("a")) == 1
    assert len(await store.get("b")) == 1
    await store.clear("a")
    assert len(await store.get("a")) == 0
    assert len(await store.get("b")) == 1


# ── ConversationMemory middleware ──────────────────────────────


@pytest.mark.asyncio
async def test_memory_injects_history():
    """History is available in msg.metadata['history'] during handler."""
    store = InMemoryStore()
    mw = ConversationMemory(store=store, max_turns=50)

    captured_history = None

    async def handler(msg: UnifiedMessage) -> str:
        nonlocal captured_history
        captured_history = msg.metadata.get("history")
        return "reply1"

    # First call — history should be empty
    await mw.process(_msg(text="msg1"), handler)
    assert captured_history == []

    # Second call — history should contain previous turn
    await mw.process(_msg(text="msg2"), handler)
    assert len(captured_history) == 2  # user msg + assistant reply
    assert captured_history[0]["role"] == "user"
    assert captured_history[0]["content"] == "msg1"
    assert captured_history[1]["role"] == "assistant"
    assert captured_history[1]["content"] == "reply1"


@pytest.mark.asyncio
async def test_memory_saves_user_and_reply():
    store = InMemoryStore()
    mw = ConversationMemory(store=store, max_turns=50)

    async def handler(msg: UnifiedMessage) -> str:
        return "bot-reply"

    await mw.process(_msg(text="user-input"), handler)
    history = await store.get("test:chat1")
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "user-input"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "bot-reply"


@pytest.mark.asyncio
async def test_memory_no_reply_saves_only_user():
    store = InMemoryStore()
    mw = ConversationMemory(store=store, max_turns=50)

    async def handler(msg: UnifiedMessage) -> None:
        return None

    await mw.process(_msg(text="hi"), handler)
    history = await store.get("test:chat1")
    assert len(history) == 1
    assert history[0]["role"] == "user"


@pytest.mark.asyncio
async def test_memory_max_turns_trimming():
    store = InMemoryStore()
    mw = ConversationMemory(store=store, max_turns=4)

    async def handler(msg: UnifiedMessage) -> str:
        return "ok"

    # Each call adds 2 entries (user + assistant)
    for i in range(5):
        await mw.process(_msg(text=f"msg{i}"), handler)

    history = await store.get("test:chat1")
    assert len(history) == 4  # trimmed to max_turns


@pytest.mark.asyncio
async def test_memory_separate_chats():
    """Different chat_ids get separate histories."""
    store = InMemoryStore()
    mw = ConversationMemory(store=store)

    async def handler(msg: UnifiedMessage) -> str:
        return "ok"

    await mw.process(_msg(text="a", chat_id="chat_a"), handler)
    await mw.process(_msg(text="b", chat_id="chat_b"), handler)

    history_a = await store.get("test:chat_a")
    history_b = await store.get("test:chat_b")
    assert len(history_a) == 2
    assert len(history_b) == 2
    assert history_a[0]["content"] == "a"
    assert history_b[0]["content"] == "b"


# ── SQLiteStore ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_store_crud():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = SQLiteStore(db_path=db_path)
        await store.append("k", {"role": "user", "content": "hi"})
        await store.append("k", {"role": "assistant", "content": "hello"})

        result = await store.get("k")
        assert len(result) == 2
        assert result[0]["content"] == "hi"

        await store.trim("k", 1)
        result = await store.get("k")
        assert len(result) == 1
        assert result[0]["content"] == "hello"

        await store.clear("k")
        result = await store.get("k")
        assert result == []

        store.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_sqlite_store_persistence():
    """Data survives re-opening the same DB file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store1 = SQLiteStore(db_path=db_path)
        await store1.append("k", {"msg": "persisted"})
        store1.close()

        store2 = SQLiteStore(db_path=db_path)
        result = await store2.get("k")
        assert len(result) == 1
        assert result[0]["msg"] == "persisted"
        store2.close()
    finally:
        os.unlink(db_path)


# ── Additional memory tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_max_turns_1():
    """max_turns=1 immediately trims to 1 entry."""
    store = InMemoryStore()
    mw = ConversationMemory(store=store, max_turns=1)

    async def handler(msg: UnifiedMessage) -> str:
        return "ok"

    # Each process adds 2 entries, trim to 1
    await mw.process(_msg(text="first"), handler)
    history = await store.get("test:chat1")
    assert len(history) == 1
    # The last entry should be the assistant reply (trimmed keeps last N)
    assert history[0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_memory_history_ordering():
    """History entries are in oldest-first order."""
    store = InMemoryStore()
    mw = ConversationMemory(store=store, max_turns=50)

    captured_histories = []

    async def handler(msg: UnifiedMessage) -> str:
        captured_histories.append(list(msg.metadata.get("history", [])))
        return "ok"

    await mw.process(_msg(text="first"), handler)
    await mw.process(_msg(text="second"), handler)
    await mw.process(_msg(text="third"), handler)

    # Third call should see: user:first, assistant:ok, user:second, assistant:ok
    history = captured_histories[2]
    assert history[0]["content"] == "first"
    assert history[1]["content"] == "ok"
    assert history[2]["content"] == "second"
    assert history[3]["content"] == "ok"


@pytest.mark.asyncio
async def test_memory_multiple_chats_dont_interfere():
    """Messages in different chats maintain separate histories."""
    store = InMemoryStore()
    mw = ConversationMemory(store=store)

    async def handler(msg: UnifiedMessage) -> str:
        return f"reply to {msg.content.text}"

    await mw.process(_msg(text="a", chat_id="chat_A"), handler)
    await mw.process(_msg(text="b", chat_id="chat_B"), handler)
    await mw.process(_msg(text="c", chat_id="chat_A"), handler)

    history_a = await store.get("test:chat_A")
    history_b = await store.get("test:chat_B")
    assert len(history_a) == 4  # 2 turns in chat_A
    assert len(history_b) == 2  # 1 turn in chat_B
    assert history_a[0]["content"] == "a"
    assert history_b[0]["content"] == "b"


@pytest.mark.asyncio
async def test_memory_clear_specific_chat():
    """Clearing one chat doesn't affect others."""
    store = InMemoryStore()
    await store.append("test:chat1", {"role": "user", "content": "hi"})
    await store.append("test:chat2", {"role": "user", "content": "hello"})

    await store.clear("test:chat1")

    assert await store.get("test:chat1") == []
    result = await store.get("test:chat2")
    assert len(result) == 1
    assert result[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_sqlite_concurrent_access():
    """Multiple operations on same SQLite store don't corrupt data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = SQLiteStore(db_path=db_path)

        # Rapid sequential operations
        for i in range(20):
            await store.append("k", {"i": i})

        result = await store.get("k")
        assert len(result) == 20

        await store.trim("k", 5)
        result = await store.get("k")
        assert len(result) == 5
        # Should have last 5 entries
        assert result[0]["i"] == 15

        store.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_inmemory_empty_history():
    """Getting empty history returns empty list, not error."""
    store = InMemoryStore()
    result = await store.get("never_used_key")
    assert result == []
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_memory_very_long_messages():
    """Very long messages don't break storage."""
    store = InMemoryStore()
    mw = ConversationMemory(store=store)

    long_text = "x" * 100_000

    async def handler(msg: UnifiedMessage) -> str:
        return long_text

    await mw.process(_msg(text=long_text), handler)
    history = await store.get("test:chat1")
    assert len(history) == 2
    assert len(history[0]["content"]) == 100_000
    assert len(history[1]["content"]) == 100_000


@pytest.mark.asyncio
async def test_memory_with_middleware_chain():
    """Memory works correctly when chained with access + command middleware."""
    from unified_channel.middleware import AccessMiddleware, CommandMiddleware

    store = InMemoryStore()
    memory_mw = ConversationMemory(store=store)
    access_mw = AccessMiddleware(allowed_user_ids={"user1"})

    async def final_handler(msg: UnifiedMessage) -> str:
        history = msg.metadata.get("history", [])
        return f"history_len={len(history)}"

    # Chain: access -> memory -> handler
    async def run_chain(msg):
        async def memory_step(m):
            return await memory_mw.process(m, final_handler)
        return await access_mw.process(msg, memory_step)

    result = await run_chain(_msg(text="first", sender_id="user1"))
    assert result == "history_len=0"

    result = await run_chain(_msg(text="second", sender_id="user1"))
    assert result == "history_len=2"

    # Blocked user
    result = await run_chain(_msg(text="blocked", sender_id="hacker"))
    assert result is None


@pytest.mark.asyncio
async def test_sqlite_store_isolation():
    """Different keys in SQLite store are independent."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = SQLiteStore(db_path=db_path)
        await store.append("a", {"v": 1})
        await store.append("b", {"v": 2})
        await store.append("a", {"v": 3})

        assert len(await store.get("a")) == 2
        assert len(await store.get("b")) == 1

        await store.clear("a")
        assert len(await store.get("a")) == 0
        assert len(await store.get("b")) == 1

        store.close()
    finally:
        os.unlink(db_path)
