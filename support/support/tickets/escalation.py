"""Escalation middleware — detects when to hand off to a human agent."""

from __future__ import annotations

import logging
from typing import Any

from unified_channel import Middleware, UnifiedMessage

from ..ai.router import AIRouter
from ..db import Database
from ..models import Agent, TicketStatus

logger = logging.getLogger(__name__)

Handler = Any


class EscalationMiddleware(Middleware):
    """Checks if escalation is needed before AI replies."""

    def __init__(self, db: Database, ai_router: AIRouter, send_fn: Any = None):
        self.db = db
        self.ai_router = ai_router
        self.send_fn = send_fn  # manager.send reference

    async def process(self, msg: UnifiedMessage, next_handler: Handler) -> Any:
        ticket = (msg.metadata or {}).get("ticket")
        if not ticket:
            return await next_handler(msg)

        text = msg.content.text or ""

        # If ticket is assigned to agent, let AgentReplyMiddleware handle it
        if ticket.status == TicketStatus.ASSIGNED:
            return await next_handler(msg)

        # Count AI turns for this ticket (efficient COUNT query)
        ai_turns = await self.db.count_messages_by_role(ticket.id, "ai")

        # Check escalation triggers
        if self.ai_router.should_escalate(text, ai_turns):
            user_lang = (msg.metadata or {}).get("user_lang")
            return await self._escalate(msg, ticket, user_lang=user_lang)

        # Otherwise, proceed to AI handler
        return await next_handler(msg)

    async def _escalate(self, msg: UnifiedMessage, ticket: Any, user_lang: str | None = None) -> str:
        """Escalate ticket to a human agent."""
        agent = await self.db.get_available_agent()

        if agent:
            await self.db.update_ticket_status(
                ticket.id, TicketStatus.ASSIGNED, agent.id
            )
            await self.db.update_agent_load(agent.id, 1)
            await self.db.log_event("escalated", ticket_id=ticket.id, agent_id=agent.id)

            # Notify agent
            if self.send_fn and agent.channel and agent.chat_id:
                customer_name = msg.sender.display_name or msg.sender.username or "Customer"
                notify = (
                    f"🎫 New ticket assigned: #{ticket.id}\n"
                    f"Customer: {customer_name}\n"
                    f"Channel: {msg.channel}\n"
                    f"Message: {msg.content.text or ''}\n\n"
                    f"Reply here to respond to the customer."
                )
                await self.send_fn(agent.channel, agent.chat_id, notify)

            logger.info("Ticket %s escalated to agent %s", ticket.id, agent.name)
            return self._escalation_msg(agent.name, available=True, lang=user_lang)
        else:
            await self.db.update_ticket_status(ticket.id, TicketStatus.ESCALATED)
            await self.db.log_event("escalated", ticket_id=ticket.id)
            logger.info("Ticket %s escalated (no agent available)", ticket.id)
            return self._escalation_msg(None, available=False, lang=user_lang)

    @staticmethod
    def _escalation_msg(agent_name: str | None, *, available: bool, lang: str | None) -> str:
        if lang and lang.startswith("zh"):
            if available:
                return f"正在为您转接人工客服，{agent_name} 马上为您服务 🙋"
            return "当前客服繁忙，您的请求已排队，稍后会有专人联系您，请耐心等待 🙏"
        if available:
            return (
                "I'm connecting you with a human agent. "
                f"{agent_name} will be with you shortly. 🙋"
            )
        return (
            "All our agents are currently busy. "
            "Your request has been queued and someone will respond soon. "
            "Thank you for your patience. 🙏"
        )
