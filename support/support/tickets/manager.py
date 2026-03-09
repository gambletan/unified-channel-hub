"""Ticket lifecycle management middleware.

Creates tickets on first customer message, tracks state,
stores all messages for dashboard visibility.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from unified_channel import Middleware, UnifiedMessage

from ..db import Database
from ..models import Ticket, TicketMessage, TicketStatus

logger = logging.getLogger(__name__)

Handler = Any  # unified_channel.Handler type


class TicketMiddleware(Middleware):
    """Creates/finds tickets per chat and logs all messages."""

    def __init__(self, db: Database):
        self.db = db

    async def process(self, msg: UnifiedMessage, next_handler: Handler) -> Any:
        channel = msg.channel
        chat_id = msg.chat_id or msg.sender.id

        # Find or create ticket
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

        # Store customer message
        await self.db.add_message(TicketMessage(
            ticket_id=ticket.id,
            role="customer",
            sender_id=msg.sender.id,
            sender_name=msg.sender.display_name or msg.sender.username,
            content=msg.content.text or "",
            channel=channel,
        ))

        # Inject ticket into metadata for downstream middleware
        if not hasattr(msg, "metadata") or msg.metadata is None:
            msg.metadata = {}
        msg.metadata["ticket"] = ticket

        # Call next handler
        result = await next_handler(msg)

        # Store AI/agent reply
        if result and isinstance(result, str):
            role = "agent" if ticket.status == TicketStatus.ASSIGNED else "ai"
            await self.db.add_message(TicketMessage(
                ticket_id=ticket.id,
                role=role,
                content=result,
                channel=channel,
            ))

            # Log first response time
            if ticket.created_at == ticket.updated_at:
                elapsed_ms = int(
                    (datetime.now(timezone.utc) - ticket.created_at).total_seconds() * 1000
                )
                await self.db.log_event(
                    "first_response", ticket_id=ticket.id, value_ms=elapsed_ms
                )

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
