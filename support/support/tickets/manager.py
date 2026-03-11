"""Ticket lifecycle management middleware.

Creates tickets on first customer message, tracks state,
stores all messages for dashboard visibility.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from unified_channel import Middleware, UnifiedMessage

from ..db import Database
from ..models import Ticket, TicketMessage, TicketStatus

logger = logging.getLogger(__name__)

Handler = Any  # unified_channel.Handler type

# TTL for ticket cache entries (seconds)
_TICKET_CACHE_TTL = 300


class TicketMiddleware(Middleware):
    """Creates/finds tickets per chat and logs all messages."""

    def __init__(self, db: Database, ai_id: str = "ai:minimax"):
        self.db = db
        self.ai_id = ai_id  # e.g. "ai:minimax:MiniMax-Text-01"
        # In-memory cache: (channel, chat_id) -> (ticket, timestamp)
        self._ticket_cache: dict[tuple[str, str], tuple[Ticket, float]] = {}

    def _get_cached_ticket(self, channel: str, chat_id: str) -> Ticket | None:
        """Return cached ticket if still valid."""
        key = (channel, chat_id)
        cached = self._ticket_cache.get(key)
        if cached:
            ticket, ts = cached
            if time.monotonic() - ts < _TICKET_CACHE_TTL:
                return ticket
            del self._ticket_cache[key]
        return None

    def _cache_ticket(self, ticket: Ticket) -> None:
        self._ticket_cache[(ticket.channel, ticket.chat_id)] = (ticket, time.monotonic())

    async def process(self, msg: UnifiedMessage, next_handler: Handler) -> Any:
        channel = msg.channel
        chat_id = msg.chat_id or msg.sender.id

        # Find or create ticket (check cache first)
        ticket = self._get_cached_ticket(channel, chat_id)
        if not ticket:
            ticket = await self.db.find_ticket_by_chat(channel, chat_id)
        if not ticket:
            # Include platform user ID if identity was bound
            platform_uid = (msg.metadata or {}).get("platform_user_id")
            display_name = msg.sender.display_name or msg.sender.username
            if platform_uid:
                display_name = f"{display_name} (#{platform_uid})"

            ticket = Ticket(
                channel=channel,
                chat_id=chat_id,
                customer_id=platform_uid or msg.sender.id,
                customer_name=display_name,
                subject=self._extract_subject(msg.content.text or ""),
                metadata={"platform_user_id": platform_uid} if platform_uid else {},
            )
            ticket = await self.db.create_ticket(ticket)
            await self.db.log_event("ticket_created", ticket_id=ticket.id)
            logger.info("New ticket %s from %s:%s", ticket.id, channel, chat_id)

        # Cache the ticket for subsequent messages
        self._cache_ticket(ticket)

        # Store customer message (no commit yet — batch with reply)
        customer_id = ticket.customer_id
        await self.db.add_message(TicketMessage(
            ticket_id=ticket.id,
            role="customer",
            sender_id=msg.sender.id,
            sender_name=msg.sender.display_name or msg.sender.username,
            content=msg.content.text or "",
            channel=channel,
            from_id=customer_id,
            to_id=self.ai_id,
        ), commit=False)

        # Inject ticket into metadata for downstream middleware
        if not hasattr(msg, "metadata") or msg.metadata is None:
            msg.metadata = {}
        msg.metadata["ticket"] = ticket

        # Call next handler
        result = await next_handler(msg)

        # Store AI/agent reply (no commit yet)
        if result and isinstance(result, str):
            is_agent = ticket.status == TicketStatus.ASSIGNED
            role = "agent" if is_agent else "ai"
            reply_from = ticket.assigned_agent_id if is_agent else self.ai_id
            await self.db.add_message(TicketMessage(
                ticket_id=ticket.id,
                role=role,
                content=result,
                channel=channel,
                from_id=reply_from,
                to_id=customer_id,
            ), commit=False)

            # Log first response time (use COUNT query instead of loading all messages)
            ai_or_agent_count = await self.db.count_messages_by_roles(
                ticket.id, ["ai", "agent"]
            )
            if ai_or_agent_count <= 1:  # This is the first reply
                elapsed_ms = int(
                    (datetime.now(timezone.utc) - ticket.created_at).total_seconds() * 1000
                )
                await self.db.log_event(
                    "first_response", ticket_id=ticket.id, value_ms=elapsed_ms,
                    commit=False,
                )

        # Single batch commit for all writes
        await self.db.commit()

        return result

    def _extract_subject(self, text: str) -> str:
        """Extract a short subject from the first message."""
        text = text.strip()
        if len(text) <= 50:
            return text
        # Try to cut at sentence boundary
        for sep in ["。", ".", "？", "?", "！", "!", "\n"]:
            idx = text.find(sep)
            if 0 < idx <= 80:
                return text[: idx + 1]
        return text[:50] + "..."
