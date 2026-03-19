"""ConversationMemory middleware — maintains per-chat conversation history."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime
from typing import Any

try:
    import aiosqlite  # preferred: truly async
    _HAS_AIOSQLITE = True
except ImportError:
    _HAS_AIOSQLITE = False

from .middleware import Handler, Middleware
from .types import UnifiedMessage


class MemoryStore(ABC):
    """Pluggable storage backend for conversation history."""

    @abstractmethod
    async def get(self, key: str) -> list[dict[str, Any]]:
        """Return full history for the given key."""

    @abstractmethod
    async def append(self, key: str, entry: dict[str, Any]) -> None:
        """Append a single entry to the history."""

    async def append_many(self, key: str, entries: list[dict[str, Any]]) -> None:
        """Append multiple entries. Override for batch-optimized writes."""
        for entry in entries:
            await self.append(key, entry)

    @abstractmethod
    async def trim(self, key: str, max_entries: int) -> None:
        """Keep only the last *max_entries* items."""

    @abstractmethod
    async def clear(self, key: str) -> None:
        """Delete all history for the given key."""


class InMemoryStore(MemoryStore):
    """Default in-memory store (dict-based). Data is lost on restart."""

    def __init__(self) -> None:
        self._data: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def get(self, key: str) -> list[dict[str, Any]]:
        return list(self._data[key])

    async def append(self, key: str, entry: dict[str, Any]) -> None:
        self._data[key].append(entry)

    async def append_many(self, key: str, entries: list[dict[str, Any]]) -> None:
        self._data[key].extend(entries)

    async def trim(self, key: str, max_entries: int) -> None:
        history = self._data[key]
        if len(history) > max_entries:
            self._data[key] = history[-max_entries:]

    async def clear(self, key: str) -> None:
        self._data.pop(key, None)


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS memory ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  key TEXT NOT NULL,"
    "  entry TEXT NOT NULL"
    ")"
)
_INDEX = "CREATE INDEX IF NOT EXISTS idx_memory_key ON memory(key)"
_WAL_MODE = "PRAGMA journal_mode=WAL"
_SYNC_NORMAL = "PRAGMA synchronous=NORMAL"


class SQLiteStore(MemoryStore):
    """SQLite-backed persistent store.

    Uses aiosqlite for truly async I/O when available, otherwise falls
    back to synchronous sqlite3 wrapped in ``run_in_executor``.
    """

    def __init__(self, db_path: str = "unified_channel_memory.db") -> None:
        self._db_path = db_path
        if _HAS_AIOSQLITE:
            self._aconn: aiosqlite.Connection | None = None  # type: ignore[assignment]
            self._sync_conn = None
        else:
            self._aconn = None
            conn = sqlite3.connect(db_path)
            conn.execute(_WAL_MODE)
            conn.execute(_SYNC_NORMAL)
            conn.execute(_SCHEMA)
            conn.execute(_INDEX)
            conn.commit()
            self._sync_conn = conn

    async def _ensure_conn(self) -> "aiosqlite.Connection":
        if self._aconn is None:
            self._aconn = await aiosqlite.connect(self._db_path)
            await self._aconn.execute(_WAL_MODE)
            await self._aconn.execute(_SYNC_NORMAL)
            await self._aconn.execute(_SCHEMA)
            await self._aconn.execute(_INDEX)
            await self._aconn.commit()
        return self._aconn

    # --- async path (aiosqlite) ---

    async def get(self, key: str) -> list[dict[str, Any]]:
        if _HAS_AIOSQLITE:
            conn = await self._ensure_conn()
            async with conn.execute(
                "SELECT entry FROM memory WHERE key = ? ORDER BY id", (key,)
            ) as cursor:
                rows = await cursor.fetchall()
            return [json.loads(row[0]) for row in rows]
        # fallback: sync sqlite3 in executor
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(
            None, self._get_sync, key
        )

    async def append(self, key: str, entry: dict[str, Any]) -> None:
        if _HAS_AIOSQLITE:
            conn = await self._ensure_conn()
            await conn.execute(
                "INSERT INTO memory (key, entry) VALUES (?, ?)",
                (key, json.dumps(entry)),
            )
            await conn.commit()
            return
        import asyncio
        await asyncio.get_running_loop().run_in_executor(
            None, self._append_sync, key, entry
        )

    async def append_many(self, key: str, entries: list[dict[str, Any]]) -> None:
        """Batch-append multiple entries in a single transaction."""
        if not entries:
            return
        rows = [(key, json.dumps(e)) for e in entries]
        if _HAS_AIOSQLITE:
            conn = await self._ensure_conn()
            await conn.executemany(
                "INSERT INTO memory (key, entry) VALUES (?, ?)", rows
            )
            await conn.commit()
            return
        import asyncio
        await asyncio.get_running_loop().run_in_executor(
            None, self._append_many_sync, rows
        )

    async def trim(self, key: str, max_entries: int) -> None:
        if _HAS_AIOSQLITE:
            conn = await self._ensure_conn()
            async with conn.execute(
                "SELECT id FROM memory WHERE key = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
                (key, max_entries),
            ) as cursor:
                ids_to_delete = [row[0] for row in await cursor.fetchall()]
            if ids_to_delete:
                placeholders = ",".join("?" for _ in ids_to_delete)
                await conn.execute(
                    f"DELETE FROM memory WHERE id IN ({placeholders})",
                    ids_to_delete,
                )
                await conn.commit()
            return
        import asyncio
        await asyncio.get_running_loop().run_in_executor(
            None, self._trim_sync, key, max_entries
        )

    async def clear(self, key: str) -> None:
        if _HAS_AIOSQLITE:
            conn = await self._ensure_conn()
            await conn.execute("DELETE FROM memory WHERE key = ?", (key,))
            await conn.commit()
            return
        import asyncio
        await asyncio.get_running_loop().run_in_executor(
            None, self._clear_sync, key
        )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        if self._sync_conn:
            self._sync_conn.close()

    async def aclose(self) -> None:
        """Async close for aiosqlite connections."""
        if self._aconn:
            await self._aconn.close()
            self._aconn = None

    # --- sync helpers (fallback when aiosqlite unavailable) ---

    def _get_sync(self, key: str) -> list[dict[str, Any]]:
        assert self._sync_conn
        cursor = self._sync_conn.execute(
            "SELECT entry FROM memory WHERE key = ? ORDER BY id", (key,)
        )
        return [json.loads(row[0]) for row in cursor.fetchall()]

    def _append_sync(self, key: str, entry: dict[str, Any]) -> None:
        assert self._sync_conn
        self._sync_conn.execute(
            "INSERT INTO memory (key, entry) VALUES (?, ?)",
            (key, json.dumps(entry)),
        )
        self._sync_conn.commit()

    def _append_many_sync(self, rows: list[tuple[str, str]]) -> None:
        assert self._sync_conn
        self._sync_conn.executemany(
            "INSERT INTO memory (key, entry) VALUES (?, ?)", rows
        )
        self._sync_conn.commit()

    def _trim_sync(self, key: str, max_entries: int) -> None:
        assert self._sync_conn
        cursor = self._sync_conn.execute(
            "SELECT id FROM memory WHERE key = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
            (key, max_entries),
        )
        ids_to_delete = [row[0] for row in cursor.fetchall()]
        if ids_to_delete:
            placeholders = ",".join("?" for _ in ids_to_delete)
            self._sync_conn.execute(
                f"DELETE FROM memory WHERE id IN ({placeholders})",
                ids_to_delete,
            )
            self._sync_conn.commit()

    def _clear_sync(self, key: str) -> None:
        assert self._sync_conn
        self._sync_conn.execute("DELETE FROM memory WHERE key = ?", (key,))
        self._sync_conn.commit()


class RedisStore(MemoryStore):
    """Redis-backed store (requires redis[hiredis])."""

    def __init__(
        self, url: str = "redis://localhost:6379", prefix: str = "uc:"
    ) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:
            raise ImportError(
                "RedisStore requires the 'redis' package: pip install redis[hiredis]"
            ) from exc
        self._redis = aioredis.from_url(url, decode_responses=True)
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> list[dict[str, Any]]:
        raw_list = await self._redis.lrange(self._key(key), 0, -1)
        return [json.loads(item) for item in raw_list]

    async def append(self, key: str, entry: dict[str, Any]) -> None:
        await self._redis.rpush(self._key(key), json.dumps(entry))

    async def append_many(self, key: str, entries: list[dict[str, Any]]) -> None:
        if entries:
            await self._redis.rpush(self._key(key), *(json.dumps(e) for e in entries))

    async def trim(self, key: str, max_entries: int) -> None:
        # Keep the last max_entries items
        await self._redis.ltrim(self._key(key), -max_entries, -1)

    async def clear(self, key: str) -> None:
        await self._redis.delete(self._key(key))


class ConversationMemory(Middleware):
    """Maintains per-chat conversation history, injected into msg.metadata["history"]."""

    def __init__(
        self,
        store: MemoryStore | None = None,
        max_turns: int = 50,
    ) -> None:
        self.store = store or InMemoryStore()
        self.max_turns = max_turns

    async def process(
        self, msg: UnifiedMessage, next_handler: Handler
    ) -> Any:
        chat_key = f"{msg.channel}:{msg.chat_id}"

        # Lazy history: inject a callable so history is only loaded if needed
        _history_cache: list[dict[str, Any]] | None = None
        store = self.store

        async def _get_history() -> list[dict[str, Any]]:
            nonlocal _history_cache
            if _history_cache is None:
                _history_cache = await store.get(chat_key)
            return _history_cache

        msg.metadata = msg.metadata or {}
        msg.metadata["get_history"] = _get_history  # lazy: await only if needed
        # Backward compat: eagerly load so msg.metadata["history"] is a plain list
        msg.metadata["history"] = await _get_history()

        result = await next_handler(msg)

        # Batch-append user message + bot reply in a single write
        entries: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": msg.content.text,
                "sender": msg.sender.id,
                "timestamp": msg.timestamp.isoformat(),
            },
        ]
        if result:
            if isinstance(result, str):
                reply_text = result
            elif hasattr(result, "text"):
                reply_text = result.text
            else:
                reply_text = str(result)
            entries.append(
                {
                    "role": "assistant",
                    "content": reply_text,
                    "timestamp": datetime.now().isoformat(),
                },
            )

        await self.store.append_many(chat_key, entries)

        # Trim to max_turns (each turn = 2 entries: user + assistant)
        await self.store.trim(chat_key, self.max_turns)
        return result
