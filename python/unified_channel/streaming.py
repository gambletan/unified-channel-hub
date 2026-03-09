"""StreamingMiddleware — typing indicators and chunked message delivery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from .middleware import Handler, Middleware
from .types import OutboundMessage, UnifiedMessage


class StreamingReply:
    """Yields text chunks for progressive delivery."""

    def __init__(self, chunks: AsyncIterator[str]) -> None:
        self._chunks = chunks

    @classmethod
    def from_llm(cls, stream: AsyncIterator[str]) -> StreamingReply:
        """Wrap an LLM streaming response."""
        return cls(chunks=stream)

    def __aiter__(self) -> AsyncIterator[str]:
        return self._chunks.__aiter__()


class StreamingMiddleware(Middleware):
    """Sends typing indicators and supports chunked message delivery.

    When the next handler returns a `StreamingReply`, the middleware collects
    chunks, sends periodic typing indicators while waiting, and delivers the
    assembled text when the stream ends.

    For regular (non-streaming) replies, typing indicators are still sent while
    the handler runs.
    """

    def __init__(
        self,
        typing_interval: float = 3.0,
        chunk_delay: float = 0.5,
    ) -> None:
        self.typing_interval = typing_interval
        self.chunk_delay = chunk_delay

    async def process(
        self, msg: UnifiedMessage, next_handler: Handler
    ) -> Any:
        adapter = msg.metadata.get("_adapter") if msg.metadata else None

        # Start typing indicator in background
        typing_task = asyncio.create_task(
            self._send_typing(adapter, msg)
        )

        try:
            result = await next_handler(msg)
        finally:
            typing_task.cancel()
            # Suppress CancelledError from the typing task
            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        # If result is a StreamingReply, collect and deliver
        if isinstance(result, StreamingReply):
            return await self._send_chunks(adapter, msg, result)

        return result

    async def _send_typing(
        self, adapter: Any, msg: UnifiedMessage
    ) -> None:
        """Periodically send typing indicators."""
        if adapter is None:
            return
        try:
            while True:
                if hasattr(adapter, "send_typing"):
                    await adapter.send_typing(msg.chat_id)
                await asyncio.sleep(self.typing_interval)
        except asyncio.CancelledError:
            pass

    async def _send_chunks(
        self,
        adapter: Any,
        msg: UnifiedMessage,
        reply: StreamingReply,
    ) -> str:
        """Collect streaming chunks and return the assembled text.

        If the adapter supports send_typing, typing indicators are sent
        between chunks.
        """
        collected: list[str] = []
        async for chunk in reply:
            collected.append(chunk)
            # Send typing between chunks if adapter supports it
            if adapter and hasattr(adapter, "send_typing") and self.chunk_delay > 0:
                await asyncio.sleep(self.chunk_delay)

        return "".join(collected)
