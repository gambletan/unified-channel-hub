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
