"""SQLite database layer for tickets, messages, agents, and knowledge base."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import (
    Agent,
    AgentStatus,
    CustomerBinding,
    KBArticle,
    Priority,
    SatisfactionRating,
    Ticket,
    TicketMessage,
    TicketStatus,
)

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    customer_name TEXT,
    subject TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT DEFAULT 'normal',
    assigned_agent_id TEXT,
    language TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_channel_chat ON tickets(channel, chat_id);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    role TEXT NOT NULL,
    sender_id TEXT,
    sender_name TEXT,
    content TEXT NOT NULL,
    channel TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ticket ON ticket_messages(ticket_id);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    channel TEXT,
    chat_id TEXT,
    status TEXT DEFAULT 'offline',
    max_concurrent INTEGER DEFAULT 5,
    current_load INTEGER DEFAULT 0,
    skills TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    UNIQUE(channel, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_bindings_platform ON customer_bindings(platform_user_id);
CREATE INDEX IF NOT EXISTS idx_bindings_channel ON customer_bindings(channel, chat_id);

CREATE TABLE IF NOT EXISTS kb_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT,
    tags TEXT,
    source_path TEXT,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    title, content, category, tags,
    content=kb_articles, content_rowid=id
);

CREATE TABLE IF NOT EXISTS satisfaction_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    rating INTEGER NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    ticket_id TEXT,
    agent_id TEXT,
    value_ms INTEGER,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analytics_type ON analytics_events(event_type, created_at);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Database:
    """Async SQLite database for the support system."""

    def __init__(self, path: str | Path = "support.db"):
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("Database connected: %s", self.path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    # ── Tickets ──

    async def create_ticket(self, ticket: Ticket) -> Ticket:
        await self.db.execute(
            """INSERT INTO tickets (id, channel, chat_id, customer_id, customer_name,
               subject, status, priority, assigned_agent_id, language,
               created_at, updated_at, resolved_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticket.id, ticket.channel, ticket.chat_id, ticket.customer_id,
                ticket.customer_name, ticket.subject, ticket.status.value,
                ticket.priority.value, ticket.assigned_agent_id, ticket.language,
                _iso(ticket.created_at), _iso(ticket.updated_at),
                _iso(ticket.resolved_at) if ticket.resolved_at else None,
                json.dumps(ticket.metadata),
            ),
        )
        await self.db.commit()
        return ticket

    async def get_ticket(self, ticket_id: str) -> Ticket | None:
        async with self.db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return self._row_to_ticket(row)

    async def find_ticket_by_chat(self, channel: str, chat_id: str) -> Ticket | None:
        """Find the most recent open/escalated/assigned ticket for a chat."""
        async with self.db.execute(
            """SELECT * FROM tickets
               WHERE channel = ? AND chat_id = ? AND status IN ('open', 'escalated', 'assigned')
               ORDER BY created_at DESC LIMIT 1""",
            (channel, chat_id),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return self._row_to_ticket(row)

    async def update_ticket_status(
        self, ticket_id: str, status: TicketStatus, agent_id: str | None = None
    ) -> None:
        now = _iso(datetime.now(timezone.utc))
        resolved = now if status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) else None
        if agent_id:
            await self.db.execute(
                """UPDATE tickets SET status = ?, assigned_agent_id = ?,
                   updated_at = ?, resolved_at = COALESCE(?, resolved_at) WHERE id = ?""",
                (status.value, agent_id, now, resolved, ticket_id),
            )
        else:
            await self.db.execute(
                """UPDATE tickets SET status = ?,
                   updated_at = ?, resolved_at = COALESCE(?, resolved_at) WHERE id = ?""",
                (status.value, now, resolved, ticket_id),
            )
        await self.db.commit()

    async def list_tickets(
        self,
        status: TicketStatus | None = None,
        channel: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ticket]:
        query = "SELECT * FROM tickets WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if channel:
            query += " AND channel = ?"
            params.append(channel)
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        async with self.db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [self._row_to_ticket(r) for r in rows]

    async def count_tickets(self, status: TicketStatus | None = None) -> int:
        if status:
            async with self.db.execute(
                "SELECT COUNT(*) FROM tickets WHERE status = ?", (status.value,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
        else:
            async with self.db.execute("SELECT COUNT(*) FROM tickets") as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    def _row_to_ticket(self, row) -> Ticket:
        return Ticket(
            id=row["id"],
            channel=row["channel"],
            chat_id=row["chat_id"],
            customer_id=row["customer_id"],
            customer_name=row["customer_name"],
            subject=row["subject"],
            status=TicketStatus(row["status"]),
            priority=Priority(row["priority"]),
            assigned_agent_id=row["assigned_agent_id"],
            language=row["language"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            resolved_at=_parse_dt(row["resolved_at"]) if row["resolved_at"] else None,
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    # ── Messages ──

    async def add_message(self, msg: TicketMessage) -> TicketMessage:
        async with self.db.execute(
            """INSERT INTO ticket_messages (ticket_id, role, sender_id, sender_name, content, channel, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (msg.ticket_id, msg.role, msg.sender_id, msg.sender_name, msg.content, msg.channel, _iso(msg.created_at)),
        ) as cur:
            msg.id = cur.lastrowid or 0
        await self.db.commit()
        return msg

    async def get_messages(self, ticket_id: str, limit: int = 100) -> list[TicketMessage]:
        async with self.db.execute(
            """SELECT * FROM ticket_messages WHERE ticket_id = ?
               ORDER BY created_at ASC LIMIT ?""",
            (ticket_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [
                TicketMessage(
                    id=r["id"], ticket_id=r["ticket_id"], role=r["role"],
                    sender_id=r["sender_id"], sender_name=r["sender_name"],
                    content=r["content"], channel=r["channel"],
                    created_at=_parse_dt(r["created_at"]),
                )
                for r in rows
            ]

    # ── Agents ──

    async def upsert_agent(self, agent: Agent) -> None:
        await self.db.execute(
            """INSERT INTO agents (id, name, email, channel, chat_id, status, max_concurrent, current_load, skills, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, email=excluded.email, channel=excluded.channel,
               chat_id=excluded.chat_id, status=excluded.status,
               max_concurrent=excluded.max_concurrent, skills=excluded.skills""",
            (
                agent.id, agent.name, agent.email, agent.channel, agent.chat_id,
                agent.status.value, agent.max_concurrent, agent.current_load,
                json.dumps(agent.skills), _iso(agent.created_at),
            ),
        )
        await self.db.commit()

    async def get_available_agent(self) -> Agent | None:
        """Get the least-loaded online agent."""
        async with self.db.execute(
            """SELECT * FROM agents WHERE status = 'online' AND current_load < max_concurrent
               ORDER BY current_load ASC LIMIT 1"""
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return self._row_to_agent(row)

    async def find_agent_by_chat(self, channel: str, chat_id: str) -> Agent | None:
        async with self.db.execute(
            "SELECT * FROM agents WHERE channel = ? AND chat_id = ?", (channel, chat_id)
        ) as cur:
            row = await cur.fetchone()
            return self._row_to_agent(row) if row else None

    async def list_agents(self) -> list[Agent]:
        async with self.db.execute("SELECT * FROM agents ORDER BY name") as cur:
            rows = await cur.fetchall()
            return [self._row_to_agent(r) for r in rows]

    async def update_agent_load(self, agent_id: str, delta: int) -> None:
        await self.db.execute(
            "UPDATE agents SET current_load = MAX(0, current_load + ?) WHERE id = ?",
            (delta, agent_id),
        )
        await self.db.commit()

    def _row_to_agent(self, row) -> Agent:
        return Agent(
            id=row["id"], name=row["name"], email=row["email"],
            channel=row["channel"], chat_id=row["chat_id"],
            status=AgentStatus(row["status"]),
            max_concurrent=row["max_concurrent"],
            current_load=row["current_load"],
            skills=json.loads(row["skills"]) if row["skills"] else [],
            created_at=_parse_dt(row["created_at"]),
        )

    # ── Knowledge Base ──

    async def index_article(self, article: KBArticle) -> KBArticle:
        async with self.db.execute(
            """INSERT INTO kb_articles (title, content, category, tags, source_path, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (article.title, article.content, article.category,
             ",".join(article.tags), article.source_path, _iso(article.updated_at)),
        ) as cur:
            article.id = cur.lastrowid or 0
        # Sync FTS
        await self.db.execute(
            "INSERT INTO kb_fts (rowid, title, content, category, tags) VALUES (?, ?, ?, ?, ?)",
            (article.id, article.title, article.content, article.category, ",".join(article.tags)),
        )
        await self.db.commit()
        return article

    async def search_kb(self, query: str, top_k: int = 3) -> list[KBArticle]:
        # FTS5: join words with OR for broad matching
        terms = query.strip().split()
        fts_query = " OR ".join(f'"{t}"' for t in terms if t)
        if not fts_query:
            return []
        async with self.db.execute(
            """SELECT kb_articles.* FROM kb_fts
               JOIN kb_articles ON kb_fts.rowid = kb_articles.id
               WHERE kb_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (fts_query, top_k),
        ) as cur:
            rows = await cur.fetchall()
            return [
                KBArticle(
                    id=r["id"], title=r["title"], content=r["content"],
                    category=r["category"],
                    tags=r["tags"].split(",") if r["tags"] else [],
                    source_path=r["source_path"],
                    updated_at=_parse_dt(r["updated_at"]),
                )
                for r in rows
            ]

    async def clear_kb(self) -> None:
        await self.db.execute("DELETE FROM kb_fts")
        await self.db.execute("DELETE FROM kb_articles")
        await self.db.commit()

    # ── Analytics ──

    async def log_event(
        self, event_type: str, ticket_id: str | None = None,
        agent_id: str | None = None, value_ms: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO analytics_events (event_type, ticket_id, agent_id, value_ms, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_type, ticket_id, agent_id, value_ms,
             json.dumps(metadata or {}), _iso(datetime.now(timezone.utc))),
        )
        await self.db.commit()

    # ── CSAT ──

    async def add_rating(self, rating: SatisfactionRating) -> None:
        await self.db.execute(
            """INSERT INTO satisfaction_ratings (ticket_id, rating, comment, created_at)
               VALUES (?, ?, ?, ?)""",
            (rating.ticket_id, rating.rating, rating.comment, _iso(rating.created_at)),
        )
        await self.db.commit()

    # ── Customer Identity Binding ──

    async def bind_customer(
        self, platform_user_id: str, channel: str, chat_id: str,
        metadata: dict | None = None,
    ) -> CustomerBinding:
        """Bind a platform user to a channel identity (upsert)."""
        now = _iso(datetime.now(timezone.utc))
        await self.db.execute(
            """INSERT INTO customer_bindings (platform_user_id, channel, chat_id, bound_at, metadata)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(channel, chat_id) DO UPDATE SET
               platform_user_id=excluded.platform_user_id, bound_at=excluded.bound_at,
               metadata=excluded.metadata""",
            (platform_user_id, channel, chat_id, now, json.dumps(metadata or {})),
        )
        await self.db.commit()
        return CustomerBinding(
            platform_user_id=platform_user_id, channel=channel,
            chat_id=chat_id, metadata=metadata or {},
        )

    async def get_binding_by_chat(self, channel: str, chat_id: str) -> CustomerBinding | None:
        """Look up which platform user is behind a channel chat."""
        async with self.db.execute(
            "SELECT * FROM customer_bindings WHERE channel = ? AND chat_id = ?",
            (channel, chat_id),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return CustomerBinding(
                id=row["id"], platform_user_id=row["platform_user_id"],
                channel=row["channel"], chat_id=row["chat_id"],
                bound_at=_parse_dt(row["bound_at"]),
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            )

    async def get_bindings_by_user(self, platform_user_id: str) -> list[CustomerBinding]:
        """Get all channel bindings for a platform user."""
        async with self.db.execute(
            "SELECT * FROM customer_bindings WHERE platform_user_id = ?",
            (platform_user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                CustomerBinding(
                    id=r["id"], platform_user_id=r["platform_user_id"],
                    channel=r["channel"], chat_id=r["chat_id"],
                    bound_at=_parse_dt(r["bound_at"]),
                    metadata=json.loads(r["metadata"]) if r["metadata"] else {},
                )
                for r in rows
            ]
