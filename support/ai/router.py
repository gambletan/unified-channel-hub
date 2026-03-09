"""AI Router — the core brain of the support system.

Receives customer messages, searches KB, calls LLM, and decides:
reply directly or escalate to human.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .backends import LLMBackend
from .rag import KnowledgeBase

logger = logging.getLogger(__name__)

# Phrases that trigger escalation
ESCALATION_PHRASES = [
    "人工", "转人工", "找人", "客服", "真人",
    "human", "agent", "person", "manager", "speak to someone",
    "talk to a human", "real person", "representative",
]

DEFAULT_SYSTEM_PROMPT = """You are a helpful customer support agent.
Answer the customer's question based on the knowledge base context provided.
Be concise, friendly, and professional.

Rules:
- If you can answer confidently from the knowledge base, do so.
- If you're not sure, say so honestly and offer to connect them with a human agent.
- Never make up information not in the knowledge base.
- Reply in the same language the customer uses.
- Keep replies under 500 words unless the question requires detail.

{kb_context}"""


class AIRouter:
    """Routes customer messages through KB search → LLM → response."""

    def __init__(
        self,
        llm: LLMBackend,
        kb: KnowledgeBase,
        system_prompt: str | None = None,
        max_ai_turns: int = 8,
        temperature: float = 0.3,
    ):
        self.llm = llm
        self.kb = kb
        self.system_prompt_template = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.max_ai_turns = max_ai_turns
        self.temperature = temperature

    def should_escalate(self, text: str, ai_turn_count: int = 0) -> bool:
        """Check if the customer wants to talk to a human."""
        text_lower = text.lower()
        # Explicit escalation request
        for phrase in ESCALATION_PHRASES:
            if phrase in text_lower:
                return True
        # Too many AI turns without resolution
        if ai_turn_count >= self.max_ai_turns:
            return True
        return False

    async def generate_reply(
        self,
        customer_text: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        """Search KB, build context, call LLM, return reply."""
        start = time.monotonic()

        # Search knowledge base
        kb_articles = await self.kb.search(customer_text)
        kb_context = self.kb.format_context(kb_articles)

        # Build system prompt
        system_prompt = self.system_prompt_template.format(kb_context=kb_context)

        # Build messages
        messages = list(history or [])
        messages.append({"role": "user", "content": customer_text})

        # Call LLM
        reply = await self.llm.complete(
            messages=messages,
            system_prompt=system_prompt,
            temperature=self.temperature,
        )

        elapsed = time.monotonic() - start
        logger.info("AI reply generated in %.1fs (KB hits: %d)", elapsed, len(kb_articles))
        return reply
