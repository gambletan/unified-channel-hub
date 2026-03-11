"""Agent reply middleware — routes agent replies back to customers."""

from __future__ import annotations

import logging
from typing import Any

from unified_channel import Middleware, UnifiedMessage

from ..db import Database
from ..models import TicketStatus

logger = logging.getLogger(__name__)

Handler = Any


class AgentReplyMiddleware(Middleware):
    """Detects messages from known agents and forwards to the customer."""

    def __init__(self, db: Database, send_fn: Any = None):
        self.db = db
        self.send_fn = send_fn  # manager.send reference

    async def process(self, msg: UnifiedMessage, next_handler: Handler) -> Any:
        # Skip if TopicBridge is handling agent routing
        if (msg.metadata or {}).get("topic_bridge"):
            return await next_handler(msg)

        # Skip non-threaded Telegram messages — agents reply via group topics, not private chat
        # Only skip for telegram; other channels (webchat, whatsapp) don't use thread_id
        if msg.channel == "telegram" and not msg.thread_id:
            return await next_handler(msg)

        # Check if sender is a registered agent
        agent = await self.db.find_agent_by_chat(msg.channel, msg.chat_id or "")
        if not agent:
            return await next_handler(msg)

        # Find the ticket assigned to this agent
        tickets = await self.db.list_tickets(status=TicketStatus.ASSIGNED)
        assigned_ticket = None
        for t in tickets:
            if t.assigned_agent_id == agent.id:
                assigned_ticket = t
                break

        if not assigned_ticket:
            return "No active ticket assigned to you."

        text = msg.content.text or ""

        # Handle agent commands
        if text.startswith("/resolve"):
            await self.db.update_ticket_status(assigned_ticket.id, TicketStatus.RESOLVED)
            await self.db.update_agent_load(agent.id, -1)
            await self.db.log_event("resolved", ticket_id=assigned_ticket.id, agent_id=agent.id)

            # Notify customer
            if self.send_fn:
                await self.send_fn(
                    assigned_ticket.channel, assigned_ticket.chat_id,
                    "Your issue has been resolved. Thank you for contacting us! "
                    "If you need further help, just send a message. 😊"
                )
            return f"Ticket #{assigned_ticket.id} resolved."

        # Forward agent reply to customer
        if self.send_fn:
            await self.send_fn(
                assigned_ticket.channel, assigned_ticket.chat_id,
                f"💬 {agent.name}: {text}"
            )

        # Store agent message
        from ..models import TicketMessage
        await self.db.add_message(TicketMessage(
            ticket_id=assigned_ticket.id,
            role="agent",
            sender_id=agent.id,
            sender_name=agent.name,
            content=text,
            channel=msg.channel,
            from_id=agent.id,
            to_id=assigned_ticket.customer_id,
        ))

        return f"Reply sent to customer (ticket #{assigned_ticket.id})"
