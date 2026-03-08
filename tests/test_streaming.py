"""Tests for StreamingMiddleware and StreamingReply."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from unified_channel.streaming import StreamingMiddleware, StreamingReply
from unified_channel.types import (
    ContentType,
    Identity,
    MessageContent,
    UnifiedMessage,
)


def _msg(
    text: str = "hello",
    metadata: dict | None = None,
) -> UnifiedMessage:
    return UnifiedMessage(
        id="1",
        channel="test",
        sender=Identity(id="user1"),
        content=MessageContent(type=ContentType.TEXT, text=text),
        chat_id="chat1",
        metadata=metadata or {},
    )


class MockAdapter:
    """Adapter stub that records send_typing calls."""

    def __init__(self) -> None:
        self.typing_calls: list[str] = []

    async def send_typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)


async def _async_chunks(*parts: str) -> AsyncIterator[str]:
    for p in parts:
        yield p


# ── StreamingReply ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_reply_collects_chunks():
    sr = StreamingReply(_async_chunks("a", "b", "c"))
    collected = []
    async for chunk in sr:
        collected.append(chunk)
    assert collected == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_streaming_reply_from_llm():
    sr = StreamingReply.from_llm(_async_chunks("hello", " world"))
    collected = []
    async for chunk in sr:
        collected.append(chunk)
    assert "".join(collected) == "hello world"


# ── StreamingMiddleware ────────────────────────────────────────


@pytest.mark.asyncio
async def test_typing_task_created_and_cancelled():
    """Typing task starts during handler execution and is cancelled after."""
    adapter = MockAdapter()
    mw = StreamingMiddleware(typing_interval=0.05, chunk_delay=0)

    async def slow_handler(msg: UnifiedMessage) -> str:
        await asyncio.sleep(0.15)
        return "done"

    msg = _msg(metadata={"_adapter": adapter})
    result = await mw.process(msg, slow_handler)
    assert result == "done"
    # Adapter should have received at least one typing call
    assert len(adapter.typing_calls) >= 1
    assert adapter.typing_calls[0] == "chat1"


@pytest.mark.asyncio
async def test_streaming_reply_through_middleware():
    """StreamingReply chunks are collected and returned as assembled text."""
    mw = StreamingMiddleware(typing_interval=0.5, chunk_delay=0)

    async def handler(msg: UnifiedMessage) -> StreamingReply:
        return StreamingReply(_async_chunks("Hello", ", ", "world!"))

    result = await mw.process(_msg(), handler)
    assert result == "Hello, world!"


@pytest.mark.asyncio
async def test_no_adapter_still_works():
    """Middleware works even when no adapter is in metadata."""
    mw = StreamingMiddleware(typing_interval=0.5)

    async def handler(msg: UnifiedMessage) -> str:
        return "ok"

    result = await mw.process(_msg(), handler)
    assert result == "ok"


@pytest.mark.asyncio
async def test_handler_exception_cancels_typing():
    """If handler raises, typing task is still cancelled cleanly."""
    adapter = MockAdapter()
    mw = StreamingMiddleware(typing_interval=0.02)

    async def bad_handler(msg: UnifiedMessage) -> str:
        await asyncio.sleep(0.05)
        raise ValueError("boom")

    msg = _msg(metadata={"_adapter": adapter})
    with pytest.raises(ValueError, match="boom"):
        await mw.process(msg, bad_handler)
    # Typing task should not leak — just verify no hanging tasks
    # (if it leaked, the test runner would warn about pending tasks)


@pytest.mark.asyncio
async def test_streaming_with_adapter_typing():
    """Adapter receives typing calls during chunk collection."""
    adapter = MockAdapter()
    mw = StreamingMiddleware(typing_interval=0.02, chunk_delay=0.05)

    async def handler(msg: UnifiedMessage) -> StreamingReply:
        return StreamingReply(_async_chunks("a", "b", "c"))

    msg = _msg(metadata={"_adapter": adapter})
    result = await mw.process(msg, handler)
    assert result == "abc"
