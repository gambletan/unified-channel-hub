"""Rate-limiting middleware — sliding window per sender."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .middleware import Handler, Middleware
from .types import OutboundMessage, UnifiedMessage


class RateLimitMiddleware(Middleware):
    """Limits how many messages a user can send within a time window.

    Uses a sliding-window algorithm: timestamps of recent messages are stored
    per key in a deque for O(1) eviction of expired entries.
    """

    def __init__(
        self,
        max_messages: int = 10,
        window_seconds: float = 60,
        key_fn: Callable[[UnifiedMessage], str] | None = None,
        reply_text: str | None = None,
    ) -> None:
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self.key_fn = key_fn or (lambda msg: msg.sender.id)
        self.reply_text = reply_text
        # key -> deque of timestamps (monotonic seconds)
        self._windows: dict[str, deque[float]] = {}
        self._process_count = 0
        self._cleanup_interval = 500  # run cleanup() every N calls

    async def process(
        self, msg: UnifiedMessage, next_handler: Handler
    ) -> str | OutboundMessage | None:
        key = self.key_fn(msg)
        now = time.monotonic()
        cutoff = now - self.window_seconds

        timestamps = self._windows.get(key)
        if timestamps is None:
            timestamps = deque()
            self._windows[key] = timestamps

        # Evict expired entries — O(1) per entry with deque
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

        if len(timestamps) >= self.max_messages:
            if self.reply_text:
                return self.reply_text
            return None

        timestamps.append(now)

        # Periodic cleanup to prevent memory leaks from inactive senders
        self._process_count += 1
        if self._process_count >= self._cleanup_interval:
            self._process_count = 0
            self.cleanup()

        return await next_handler(msg)

    def cleanup(self) -> None:
        """Remove expired entries from all tracked keys."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        to_delete = []
        for key, timestamps in self._windows.items():
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if not timestamps:
                to_delete.append(key)
        for key in to_delete:
            del self._windows[key]

    def reset(self) -> None:
        """Reset all rate limit state."""
        self._windows.clear()
