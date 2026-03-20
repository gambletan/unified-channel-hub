"""Persistent message queue backed by SQLite with at-least-once delivery."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from .middleware import Handler, Middleware
from .types import OutboundMessage, UnifiedMessage

logger = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS queue_items (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    message_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    retries INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_retry_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_items (status);
CREATE INDEX IF NOT EXISTS idx_queue_pending ON queue_items (status, next_retry_at, priority);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def _serialize_outbound(msg: OutboundMessage) -> str:
    """Serialize an OutboundMessage to JSON."""
    data: dict[str, Any] = {
        "chat_id": msg.chat_id,
        "text": msg.text,
        "reply_to_id": msg.reply_to_id,
        "thread_id": msg.thread_id,
        "media_url": msg.media_url,
        "media_type": msg.media_type,
        "parse_mode": msg.parse_mode,
        "metadata": msg.metadata,
    }
    # Serialize buttons if present
    if msg.buttons:
        data["buttons"] = [
            [{"label": b.label, "callback_data": b.callback_data, "url": b.url} for b in row]
            for row in msg.buttons
        ]
    return json.dumps(data)


def _deserialize_outbound(s: str) -> OutboundMessage:
    """Deserialize an OutboundMessage from JSON."""
    from .types import Button

    data = json.loads(s)
    buttons = None
    if data.get("buttons"):
        buttons = [
            [Button(label=b["label"], callback_data=b.get("callback_data"), url=b.get("url")) for b in row]
            for row in data["buttons"]
        ]
    return OutboundMessage(
        chat_id=data["chat_id"],
        text=data.get("text", ""),
        reply_to_id=data.get("reply_to_id"),
        thread_id=data.get("thread_id"),
        media_url=data.get("media_url"),
        media_type=data.get("media_type"),
        parse_mode=data.get("parse_mode"),
        buttons=buttons,
        metadata=data.get("metadata", {}),
    )


@dataclass
class QueueItem:
    """A single item in the persistent queue."""

    id: str
    message: OutboundMessage
    channel: str
    priority: int
    status: str  # "pending", "processing", "completed", "dead"
    retries: int
    created_at: datetime
    next_retry_at: datetime | None
    last_error: str | None


class SQLiteQueue:
    """Persistent message queue backed by SQLite.

    Provides at-least-once delivery guarantee:
    - Messages survive process restarts
    - Failed sends are retried with exponential backoff
    - Dead letter queue for permanently failed messages

    Example:
        queue = SQLiteQueue("messages.db")
        await queue.enqueue(OutboundMessage(chat_id="123", text="hello"), channel="telegram")

        # Process pending messages
        async for item in queue.pending():
            try:
                await adapter.send(item.message)
                await queue.ack(item.id)
            except Exception:
                await queue.nack(item.id)  # will retry later
    """

    def __init__(self, db_path: str, *, max_retries: int = 5, retry_delay: float = 60.0) -> None:
        self._db_path = db_path
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._db: aiosqlite.Connection | None = None

    async def _connect(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.executescript(_SCHEMA)
            await self._db.commit()
        return self._db

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def enqueue(self, msg: OutboundMessage, *, channel: str, priority: int = 0) -> str:
        """Add message to queue. Returns queue item ID."""
        db = await self._connect()
        item_id = uuid.uuid4().hex
        now = _now()
        await db.execute(
            """INSERT INTO queue_items
               (id, channel, message_json, priority, status, retries, max_retries,
                created_at, updated_at, next_retry_at, last_error)
               VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, NULL)""",
            (
                item_id,
                channel,
                _serialize_outbound(msg),
                priority,
                self._max_retries,
                _isoformat(now),
                _isoformat(now),
                _isoformat(now),  # ready immediately
            ),
        )
        await db.commit()
        return item_id

    async def pending(self, limit: int = 10) -> list[QueueItem]:
        """Get pending messages ready for delivery.

        Atomically transitions items from 'pending' to 'processing' so that
        concurrent consumers do not pick up the same items.
        """
        db = await self._connect()
        now = _isoformat(_now())
        cursor = await db.execute(
            """SELECT id, channel, message_json, priority, status, retries,
                      created_at, next_retry_at, last_error
               FROM queue_items
               WHERE status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY priority DESC, created_at ASC
               LIMIT ?""",
            (now, limit),
        )
        rows = await cursor.fetchall()
        if not rows:
            return []

        # Batch-update all fetched rows to 'processing' in one statement
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE queue_items SET status = 'processing', updated_at = ? WHERE id IN ({placeholders})",
            [now, *ids],
        )
        await db.commit()

        return [
            QueueItem(
                id=row["id"],
                message=_deserialize_outbound(row["message_json"]),
                channel=row["channel"],
                priority=row["priority"],
                status="processing",
                retries=row["retries"],
                created_at=_parse_iso(row["created_at"]),  # type: ignore[arg-type]
                next_retry_at=_parse_iso(row["next_retry_at"]),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    async def ack(self, item_id: str) -> None:
        """Acknowledge successful delivery."""
        db = await self._connect()
        now = _isoformat(_now())
        await db.execute(
            "UPDATE queue_items SET status = 'completed', updated_at = ? WHERE id = ?",
            (now, item_id),
        )
        await db.commit()

    async def nack(self, item_id: str, *, error: str = "") -> None:
        """Mark delivery as failed. Will retry with backoff, or move to dead letter."""
        db = await self._connect()
        now = _now()

        cursor = await db.execute(
            "SELECT retries, max_retries FROM queue_items WHERE id = ?", (item_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return

        retries = row["retries"] + 1
        max_retries = row["max_retries"]

        if retries >= max_retries:
            # Move to dead letter queue
            await db.execute(
                """UPDATE queue_items
                   SET status = 'dead', retries = ?, last_error = ?, updated_at = ?
                   WHERE id = ?""",
                (retries, error, _isoformat(now), item_id),
            )
        else:
            # Retry with exponential backoff
            delay = self._retry_delay * (2 ** retries)
            next_retry = now + timedelta(seconds=delay)
            await db.execute(
                """UPDATE queue_items
                   SET status = 'pending', retries = ?, last_error = ?,
                       next_retry_at = ?, updated_at = ?
                   WHERE id = ?""",
                (retries, error, _isoformat(next_retry), _isoformat(now), item_id),
            )
        await db.commit()

    async def dead_letters(self, limit: int = 50) -> list[QueueItem]:
        """Get permanently failed messages."""
        db = await self._connect()
        cursor = await db.execute(
            """SELECT id, channel, message_json, priority, status, retries,
                      created_at, next_retry_at, last_error
               FROM queue_items
               WHERE status = 'dead'
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            QueueItem(
                id=row["id"],
                message=_deserialize_outbound(row["message_json"]),
                channel=row["channel"],
                priority=row["priority"],
                status="dead",
                retries=row["retries"],
                created_at=_parse_iso(row["created_at"]),  # type: ignore[arg-type]
                next_retry_at=_parse_iso(row["next_retry_at"]),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    async def stats(self) -> dict:
        """Queue statistics: pending, processing, completed, dead, total."""
        db = await self._connect()
        cursor = await db.execute(
            "SELECT status, COUNT(*) as cnt FROM queue_items GROUP BY status"
        )
        rows = await cursor.fetchall()
        counts = {row["status"]: row["cnt"] for row in rows}
        total = sum(counts.values())
        return {
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0),
            "completed": counts.get("completed", 0),
            "dead": counts.get("dead", 0),
            "total": total,
        }

    async def purge_completed(self, older_than_hours: int = 24) -> int:
        """Remove completed messages older than N hours."""
        db = await self._connect()
        cutoff = _now() - timedelta(hours=older_than_hours)
        cursor = await db.execute(
            "DELETE FROM queue_items WHERE status = 'completed' AND updated_at < ?",
            (_isoformat(cutoff),),
        )
        await db.commit()
        return cursor.rowcount


class PersistentQueueMiddleware(Middleware):
    """Middleware that queues outbound messages for reliable delivery.

    Instead of sending directly, messages go through the persistent queue
    with retry logic and delivery guarantees.

    This middleware intercepts inbound messages, calls the next handler to get
    a reply, and if a reply is produced, enqueues it for persistent delivery.
    """

    def __init__(self, queue: SQLiteQueue, *, default_channel: str = "default") -> None:
        self._queue = queue
        self._default_channel = default_channel

    async def process(
        self, msg: UnifiedMessage, next_handler: Handler
    ) -> str | OutboundMessage | None:
        result = await next_handler(msg)
        if result is None:
            return None

        # Convert string replies to OutboundMessage
        if isinstance(result, str):
            if msg.chat_id is None:
                return result  # can't queue without chat_id
            outbound = OutboundMessage(chat_id=msg.chat_id, text=result)
        else:
            outbound = result

        channel = msg.channel or self._default_channel
        item_id = await self._queue.enqueue(outbound, channel=channel)
        logger.debug("Queued message %s for channel %s", item_id, channel)
        # Return None since delivery will happen asynchronously via queue processing
        return None
