"""AI Router — the core brain of the support system.

Receives customer messages, searches KB, calls LLM, and decides:
reply directly or escalate to human.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from .backends import LLMBackend, StreamCallback
from .rag import KnowledgeBase

logger = logging.getLogger(__name__)

# Phrases that trigger escalation
ESCALATION_PHRASES = [
    "人工", "转人工", "找人", "客服", "真人",
    "human", "agent", "person", "manager", "speak to someone",
    "talk to a human", "real person", "representative",
]

# Short/trivial messages that don't need KB search
_TRIVIAL_PATTERNS = {
    "hi", "hello", "hey", "你好", "嗨", "在吗", "在不在",
    "ok", "好的", "好", "谢谢", "thanks", "thank you", "thx",
    "嗯", "哦", "明白", "收到", "了解", "知道了",
    "bye", "再见", "拜拜", "88",
}

DEFAULT_SYSTEM_PROMPT = """You are a helpful customer support agent.
You can ONLY answer questions based on the knowledge base context below.
Be concise, friendly, and professional.

Rules:
- ONLY use information from the knowledge base context to answer. Do NOT use your own knowledge.
- If the knowledge base context is empty or does not contain relevant information, say you don't have that information and offer to connect them with a human agent. Say "转人工" or "human agent" — do NOT guess or make up an answer.
- Never make up information not in the knowledge base.
- Reply in the same language the customer uses.
- Keep replies under 500 words unless the question requires detail.
- For greetings (hi, hello, 你好), respond warmly and ask how you can help.

{kb_context}"""

# FAQ cache TTL in seconds
_FAQ_CACHE_TTL = 600
_FAQ_CACHE_MAX = 100


class AIRouter:
    """Routes customer messages through KB search → LLM → response."""

    def __init__(
        self,
        llm: LLMBackend,
        kb: KnowledgeBase,
        system_prompt: str | None = None,
        max_ai_turns: int = 8,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ):
        self.llm = llm
        self.kb = kb
        self.system_prompt_template = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.max_ai_turns = max_ai_turns
        self.temperature = temperature
        self.max_tokens = max_tokens
        # FAQ cache: cache_key -> (reply, timestamp)
        self._faq_cache: dict[str, tuple[str, float]] = {}

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

    @staticmethod
    def _is_trivial(text: str) -> bool:
        """Check if message is too short/trivial to need KB search."""
        normalized = text.strip().lower().rstrip("?？!！。.~")
        return normalized in _TRIVIAL_PATTERNS or len(normalized) <= 2

    @staticmethod
    def _faq_key(text: str, user_context: str | None) -> str:
        """Build a cache key for FAQ. Ignores history (FAQ = first-turn answers)."""
        normalized = text.strip().lower()
        ctx = user_context or ""
        return hashlib.md5(f"{normalized}|{ctx}".encode()).hexdigest()

    def _get_faq_cache(self, key: str) -> str | None:
        """Return cached FAQ reply if still valid."""
        cached = self._faq_cache.get(key)
        if cached:
            reply, ts = cached
            if time.monotonic() - ts < _FAQ_CACHE_TTL:
                return reply
            del self._faq_cache[key]
        return None

    def _set_faq_cache(self, key: str, reply: str) -> None:
        """Cache a FAQ reply. Evict old entries if cache is full."""
        self._faq_cache[key] = (reply, time.monotonic())
        if len(self._faq_cache) > _FAQ_CACHE_MAX:
            now = time.monotonic()
            self._faq_cache = {
                k: v for k, v in self._faq_cache.items()
                if now - v[1] < _FAQ_CACHE_TTL
            }

    def _build_prompt(
        self,
        customer_text: str,
        kb_context: str,
        user_context: str | None = None,
        user_lang: str | None = None,
    ) -> str:
        """Build the system prompt with KB context, language, and user info."""
        system_prompt = self.system_prompt_template.format(kb_context=kb_context)

        if user_lang:
            lang_name = {"en": "English", "zh": "Chinese", "fr": "French", "es": "Spanish", "ja": "Japanese", "ko": "Korean", "de": "German", "pt": "Portuguese", "ar": "Arabic", "ru": "Russian", "th": "Thai", "vi": "Vietnamese"}.get(user_lang, user_lang)
            system_prompt += f"\n\nIMPORTANT: You MUST reply in {lang_name}. Do NOT use any other language."

        if user_context:
            system_prompt += f"\n\n--- Customer Info ---\n{user_context}\n--- End Customer Info ---"

        return system_prompt

    async def generate_reply(
        self,
        customer_text: str,
        history: list[dict[str, str]] | None = None,
        user_context: str | None = None,
        user_lang: str | None = None,
        on_chunk: StreamCallback | None = None,
    ) -> str:
        """Search KB, build context, call LLM, return reply.

        Args:
            user_context: Optional ERP user/order info to inject into prompt.
            user_lang: Detected language code (e.g. "en", "zh") to force reply language.
            on_chunk: If provided, use streaming mode and call this on each chunk.
        """
        start = time.monotonic()

        # FAQ cache check (only for first-turn, no history)
        is_first_turn = not history or len(history) == 0
        faq_key = self._faq_key(customer_text, user_context) if is_first_turn else None
        if faq_key:
            cached_reply = self._get_faq_cache(faq_key)
            if cached_reply:
                logger.info("FAQ cache hit (%.1fms)", (time.monotonic() - start) * 1000)
                return cached_reply

        # Skip KB search for trivial messages (greetings, acknowledgements)
        if self._is_trivial(customer_text):
            kb_context = ""
            kb_hits = 0
        else:
            kb_articles = await self.kb.search(customer_text)
            kb_context = self.kb.format_context(kb_articles)
            kb_hits = len(kb_articles)

        # Build system prompt
        system_prompt = self._build_prompt(customer_text, kb_context, user_context, user_lang)

        # Build messages
        messages = list(history or [])
        messages.append({"role": "user", "content": customer_text})

        # Call LLM (streaming if callback provided)
        if on_chunk and hasattr(self.llm, "stream"):
            reply = await self.llm.stream(
                messages=messages,
                system_prompt=system_prompt,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                on_chunk=on_chunk,
            )
        else:
            reply = await self.llm.complete(
                messages=messages,
                system_prompt=system_prompt,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

        # Cache first-turn replies as FAQ
        if faq_key:
            self._set_faq_cache(faq_key, reply)

        elapsed = time.monotonic() - start
        logger.info(
            "AI reply in %.1fs (KB: %d, FAQ: %s, stream: %s)",
            elapsed, kb_hits, "miss" if faq_key else "n/a", bool(on_chunk),
        )
        return reply
