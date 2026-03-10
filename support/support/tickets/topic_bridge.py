"""Topic Bridge middleware — maps each customer to a forum topic in the agent group.

Based on the original customer_service_poc.py design:
- Customer DMs bot → create/find forum topic in agent group → forward message
- Agent replies in topic → forward reply to customer DM (auto-translated)
- AI auto-reply also shown in topic for agent visibility
- Auto language detection + bidirectional translation
- Reply timeout alerts
- Sensitive word filtering
- Agent commands (/close, /history, /lang)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from unified_channel import Middleware, UnifiedMessage
from unified_channel.adapters.telegram import TelegramAdapter
from unified_channel.types import ContentType

from ..ai.model_router import ModelRouter
from ..db import Database
from ..models import TicketMessage, TicketStatus

logger = logging.getLogger(__name__)

Handler = Any

# Sensitive words (basic list, extend in config)
_DEFAULT_SENSITIVE = [
    "傻逼", "操你", "fuck", "shit", "dick", "asshole",
    "滚蛋", "去死", "废物", "垃圾",
]

_LANG_NAMES = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "th": "Thai", "ar": "Arabic", "ru": "Russian", "es": "Spanish",
    "fr": "French", "de": "German", "pt": "Portuguese", "vi": "Vietnamese",
    "id": "Indonesian", "ms": "Malay", "tl": "Filipino",
}


class TopicBridgeMiddleware(Middleware):
    """Bridges customer DMs ↔ agent group forum topics.

    Features:
    - Auto topic creation per customer
    - Language detection + auto-translation (customer↔agent)
    - Sensitive word alerts
    - Reply timeout reminders
    - Agent commands (/close, /history, /lang)
    """

    def __init__(
        self,
        db: Database,
        tg_adapter: TelegramAdapter,
        group_chat_id: int,
        router: ModelRouter,
        agent_ids: set[str] | None = None,
        default_lang: str = "zh",
        reply_timeout: int = 180,
        sensitive_words: list[str] | None = None,
    ):
        self.db = db
        self.tg = tg_adapter
        self.group_chat_id = group_chat_id
        self.router = router
        self.agent_ids = agent_ids or set()
        self.default_lang = default_lang
        self.reply_timeout = reply_timeout
        self.sensitive_words = sensitive_words or _DEFAULT_SENSITIVE

        # Caches
        self._topic_cache: dict[str, int] = {}      # customer_chat_id → thread_id
        self._reverse_cache: dict[int, str] = {}     # thread_id → customer_chat_id
        self._user_lang: dict[str, str] = {}         # customer_chat_id → lang code
        self._pending_timers: dict[str, asyncio.Task] = {}  # session → timeout task

    @property
    def bot(self):
        return self.tg._app.bot

    async def process(self, msg: UnifiedMessage, next_handler: Handler) -> Any:
        chat_id = msg.chat_id or ""
        sender_id = msg.sender.id if msg.sender else ""
        text = msg.content.text or ""

        # Handle rating callbacks
        if msg.content.type == ContentType.CALLBACK and msg.content.callback_data:
            return await self._handle_callback(msg)

        if not text.strip():
            return await next_handler(msg)

        # --- Message from agent group ---
        if str(chat_id) == str(self.group_chat_id):
            return await self._handle_group_message(msg, sender_id, text)

        # --- Customer DM ---
        return await self._handle_customer_dm(msg, next_handler, text)

    # =========================================================================
    # Rating Callback
    # =========================================================================

    async def _handle_callback(self, msg: UnifiedMessage) -> Any:
        data = msg.content.callback_data or ""
        if not data.startswith("rate:"):
            return None

        parts = data.split(":")
        if len(parts) != 3:
            return None

        ticket_id, rating_str = parts[1], parts[2]
        try:
            rating = int(rating_str)
        except ValueError:
            return None

        from ..models import SatisfactionRating
        await self.db.add_rating(SatisfactionRating(
            ticket_id=ticket_id, rating=rating,
        ))
        await self.db.log_event("rated", ticket_id=ticket_id)

        # Notify in topic
        customer_chat_id = msg.chat_id or ""
        topic_id = self._topic_cache.get(customer_chat_id)
        if topic_id:
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"⭐ 用户评分: {'⭐' * rating} ({rating}/5)",
                )
            except Exception:
                pass

        # Answer callback and update message
        if msg.raw:
            try:
                query = msg.raw.callback_query
                if query:
                    await query.answer(f"感谢评分！{'⭐' * rating}")
                    await query.edit_message_text(f"感谢您的评价！{'⭐' * rating} ({rating}/5)")
            except Exception:
                pass

        logger.info("Rating %d for ticket %s", rating, ticket_id)
        return None

    # =========================================================================
    # Customer DM → Topic
    # =========================================================================

    async def _handle_customer_dm(
        self, msg: UnifiedMessage, next_handler: Handler, text: str
    ) -> Any:
        chat_id = msg.chat_id or ""
        customer_name = (
            msg.sender.display_name or msg.sender.username or chat_id
            if msg.sender else chat_id
        )

        topic_id = await self._get_or_create_topic(chat_id, customer_name)

        # Mark for downstream middleware to skip agent detection
        if not hasattr(msg, "metadata") or msg.metadata is None:
            msg.metadata = {}
        msg.metadata["topic_bridge"] = True

        # Detect language (only update away from default once, to avoid flapping)
        lang = await self._detect_language(text)
        prev_lang = self._user_lang.get(chat_id, self.default_lang)
        if prev_lang == self.default_lang and lang != self.default_lang:
            self._user_lang[chat_id] = lang
            await self._save_user_lang(chat_id, lang)

        # Sensitive word check
        matched = self._check_sensitive(text)
        if matched:
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"⚠️ 敏感词检测: {', '.join(matched)}",
                )
            except Exception:
                pass

        # Translate for agent if not default language
        display_text = text
        if lang != self.default_lang and self.router.get_backend("translate"):
            translated = await self._translate(text, self.default_lang, lang)
            if translated and translated != text:
                display_text = f"{text}\n\n🌐 _{translated}_"

        # Forward to topic
        try:
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=topic_id,
                text=f"👤 {display_text}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("Failed to forward to topic %s: %s", topic_id, e)
            # Retry without markdown
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"👤 {text}",
                )
            except Exception:
                pass

        # Start reply timeout
        self._start_timer(chat_id, topic_id)

        # Check "转人工" / "agent" request
        if text.strip().lower() in ("转人工", "agent", "human"):
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text="🔔 用户请求转人工，请尽快回复。",
                )
            except Exception:
                pass
            return "正在为您转接人工客服，请稍候。🙋"

        # Run AI pipeline
        result = await next_handler(msg)

        # Forward AI reply to topic
        if result and isinstance(result, str):
            self._cancel_timer(chat_id)
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"🤖 {result}",
                )
            except Exception as e:
                logger.error("Failed to forward AI reply to topic: %s", e)

        return result

    # =========================================================================
    # Agent Topic → Customer DM
    # =========================================================================

    async def _handle_group_message(
        self, msg: UnifiedMessage, sender_id: str, text: str
    ) -> Any:
        thread_id = int(msg.thread_id) if msg.thread_id else None
        if not thread_id:
            return None

        # Skip non-agents
        if self.agent_ids and sender_id not in self.agent_ids:
            return None

        # Agent commands
        if msg.content.type == ContentType.COMMAND or text.startswith("/"):
            return await self._handle_agent_command(msg, thread_id, text)

        customer_chat_id = self._reverse_cache.get(thread_id)
        if not customer_chat_id:
            customer_chat_id = await self._load_customer_for_topic(thread_id)
        if not customer_chat_id:
            logger.warning("No customer found for topic thread %s", thread_id)
            return None

        # Cancel timeout
        self._cancel_timer(customer_chat_id)

        # Auto-translate agent reply to user's language
        user_lang = self._user_lang.get(customer_chat_id, self.default_lang)
        send_text = text

        if user_lang != self.default_lang and self.router.get_backend("translate"):
            translated = await self._translate(text, user_lang, self.default_lang)
            if translated and translated != text:
                send_text = translated
                # Show translation in topic
                try:
                    await self.bot.send_message(
                        chat_id=self.group_chat_id,
                        message_thread_id=thread_id,
                        text=f"🌐 已翻译为 [{user_lang}]: _{translated}_",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

        # Forward to customer
        try:
            await self.bot.send_message(chat_id=int(customer_chat_id), text=send_text)
            # Store agent message via DB layer
            ticket = await self.db.find_ticket_by_chat("telegram", customer_chat_id)
            if ticket:
                await self.db.add_message(TicketMessage(
                    ticket_id=ticket.id,
                    role="agent",
                    sender_id=sender_id,
                    content=text,
                    channel="telegram",
                ))

            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                text="✅ 已发送给用户",
            )
            logger.info("Agent %s reply forwarded to %s", sender_id, customer_chat_id)
        except Exception as e:
            logger.error("Failed to forward to customer %s: %s", customer_chat_id, e)
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=thread_id,
                    text=f"❌ 发送失败: {e}",
                )
            except Exception:
                pass

        return None

    # =========================================================================
    # Agent Commands
    # =========================================================================

    async def _handle_agent_command(self, msg: UnifiedMessage, thread_id: int, text: str) -> Any:
        parts = text.strip().split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1:]

        customer_chat_id = self._reverse_cache.get(thread_id)
        if not customer_chat_id:
            customer_chat_id = await self._load_customer_for_topic(thread_id)

        if cmd == "close":
            # Close ticket
            if customer_chat_id:
                ticket = await self.db.find_ticket_by_chat("telegram", customer_chat_id)
                if ticket and ticket.status != TicketStatus.CLOSED:
                    await self.db.update_ticket_status(ticket.id, TicketStatus.CLOSED)
                    if ticket.assigned_agent_id:
                        await self.db.update_agent_load(ticket.assigned_agent_id, -1)
                    await self.db.log_event("closed", ticket_id=ticket.id)
                self._cancel_timer(customer_chat_id)
                # Notify customer with rating buttons
                try:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("⭐1", callback_data=f"rate:{ticket.id}:1"),
                            InlineKeyboardButton("⭐2", callback_data=f"rate:{ticket.id}:2"),
                            InlineKeyboardButton("⭐3", callback_data=f"rate:{ticket.id}:3"),
                            InlineKeyboardButton("⭐4", callback_data=f"rate:{ticket.id}:4"),
                            InlineKeyboardButton("⭐5", callback_data=f"rate:{ticket.id}:5"),
                        ]
                    ])
                    await self.bot.send_message(
                        chat_id=int(customer_chat_id),
                        text="您的会话已结束。请为本次服务评分：",
                        reply_markup=keyboard,
                    )
                except Exception:
                    try:
                        await self.bot.send_message(
                            chat_id=int(customer_chat_id),
                            text="您的会话已结束。如有新问题，随时发消息联系我们。👋",
                        )
                    except Exception:
                        pass
            # Close topic
            try:
                await self.bot.close_forum_topic(
                    chat_id=self.group_chat_id,
                    message_thread_id=thread_id,
                )
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=thread_id,
                    text="🔒 会话已关闭",
                )
            except Exception as e:
                logger.error("Failed to close topic: %s", e)

        elif cmd == "history":
            # Show message history
            if customer_chat_id:
                ticket = await self.db.find_ticket_by_chat("telegram", customer_chat_id)
                messages = await self.db.get_messages(ticket.id) if ticket else []
                if messages:
                    lines = []
                    for m in messages[-20:]:
                        icon = {"customer": "👤", "ai": "🤖", "agent": "💬"}.get(m.role, "•")
                        ts = m.created_at.strftime("%m-%d %H:%M") if m.created_at else ""
                        lines.append(f"{icon} [{ts}] {m.content[:100]}")
                    try:
                        await self.bot.send_message(
                            chat_id=self.group_chat_id,
                            message_thread_id=thread_id,
                            text="\n".join(lines),
                        )
                    except Exception:
                        pass
                else:
                    await self.bot.send_message(
                        chat_id=self.group_chat_id,
                        message_thread_id=thread_id,
                        text="📭 无历史消息",
                    )

        elif cmd == "lang":
            # Set/view user language
            if customer_chat_id:
                if args:
                    new_lang = args[0].lower()
                    self._user_lang[customer_chat_id] = new_lang
                    await self._save_user_lang(customer_chat_id, new_lang)
                    await self.bot.send_message(
                        chat_id=self.group_chat_id,
                        message_thread_id=thread_id,
                        text=f"✅ 用户语言已设为: {new_lang} ({_LANG_NAMES.get(new_lang, new_lang)})",
                    )
                else:
                    cur_lang = self._user_lang.get(customer_chat_id, self.default_lang)
                    await self.bot.send_message(
                        chat_id=self.group_chat_id,
                        message_thread_id=thread_id,
                        text=f"🌐 当前用户语言: {cur_lang} ({_LANG_NAMES.get(cur_lang, cur_lang)})\n用法: /lang en",
                    )

        elif cmd == "help":
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                text=(
                    "📋 可用命令:\n"
                    "/close — 关闭此会话\n"
                    "/history — 查看历史消息\n"
                    "/lang [code] — 查看/设置用户语言\n"
                    "/help — 显示此帮助"
                ),
            )

        return None

    # =========================================================================
    # Language Detection & Translation
    # =========================================================================

    async def _detect_language(self, text: str) -> str:
        if not text.strip():
            return self.default_lang

        # Heuristic for non-Latin scripts
        chinese_ratio = len(re.findall(r'[\u4e00-\u9fff]', text)) / max(len(text), 1)
        if chinese_ratio > 0.3:
            return "zh"
        if re.search(r'[\u3040-\u309f\u30a0-\u30ff]', text):
            return "ja"
        if re.search(r'[\uac00-\ud7af]', text):
            return "ko"
        if re.search(r'[\u0e00-\u0e7f]', text):
            return "th"
        if re.search(r'[\u0600-\u06ff]', text):
            return "ar"
        if re.search(r'[\u0400-\u04ff]', text):
            return "ru"

        # Latin script — use LLM
        if self.router.get_backend("detect_lang") and re.search(r'[a-zA-Z]{3,}', text):
            try:
                code = await self.router.chat(
                    "detect_lang",
                    [
                        {"role": "system", "content": "Detect the language. Reply with ONLY the ISO 639-1 code (en, fr, de, es, pt, vi, id). Nothing else."},
                        {"role": "user", "content": text},
                    ],
                    temperature=0,
                    max_tokens=5,
                    timeout=5,
                )
                code = code.strip().lower()[:2]
                if re.match(r'^[a-z]{2}$', code):
                    return code
            except Exception as e:
                logger.warning("Language detection failed: %s", e)

        if re.search(r'[a-zA-Z]{3,}', text):
            return "en"
        return self.default_lang

    async def _translate(self, text: str, target_lang: str, source_lang: str = "") -> str:
        if not text.strip() or source_lang == target_lang:
            return text
        target_name = _LANG_NAMES.get(target_lang, target_lang)
        try:
            return await self.router.chat(
                "translate",
                [
                    {"role": "system", "content": f"Translate to {target_name}. Only output the translation."},
                    {"role": "user", "content": text},
                ],
                temperature=0.1,
                max_tokens=1000,
                timeout=10,
            )
        except Exception as e:
            logger.warning("Translation failed: %s", e)
            return text

    # =========================================================================
    # Sensitive Word Filter
    # =========================================================================

    def _check_sensitive(self, text: str) -> list[str]:
        text_lower = text.lower()
        return [w for w in self.sensitive_words if w.lower() in text_lower]

    # =========================================================================
    # Reply Timeout
    # =========================================================================

    def _start_timer(self, customer_chat_id: str, topic_id: int) -> None:
        self._cancel_timer(customer_chat_id)
        self._pending_timers[customer_chat_id] = asyncio.create_task(
            self._timeout_alert(customer_chat_id, topic_id)
        )

    def _cancel_timer(self, customer_chat_id: str) -> None:
        task = self._pending_timers.pop(customer_chat_id, None)
        if task:
            task.cancel()

    async def _timeout_alert(self, customer_chat_id: str, topic_id: int) -> None:
        await asyncio.sleep(self.reply_timeout)
        if customer_chat_id not in self._pending_timers:
            return
        self._pending_timers.pop(customer_chat_id, None)
        minutes = self.reply_timeout // 60
        try:
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=topic_id,
                text=f"⏰ 用户已等待 {minutes} 分钟未收到回复！请尽快处理。",
            )
        except Exception:
            pass

    # =========================================================================
    # Topic Management
    # =========================================================================

    async def _get_or_create_topic(self, customer_chat_id: str, customer_name: str) -> int:
        if customer_chat_id in self._topic_cache:
            return self._topic_cache[customer_chat_id]

        thread_id = await self._load_topic_from_db(customer_chat_id)
        if thread_id:
            self._topic_cache[customer_chat_id] = thread_id
            self._reverse_cache[thread_id] = customer_chat_id
            # Load saved language
            lang = await self._load_user_lang(customer_chat_id)
            if lang:
                self._user_lang[customer_chat_id] = lang
            return thread_id

        try:
            topic = await self.bot.create_forum_topic(
                chat_id=self.group_chat_id,
                name=f"👤 {customer_name}",
            )
            thread_id = topic.message_thread_id
            self._topic_cache[customer_chat_id] = thread_id
            self._reverse_cache[thread_id] = customer_chat_id

            await self._save_topic_mapping(customer_chat_id, thread_id)

            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                text=(
                    f"📋 新会话\n"
                    f"• 用户: {customer_name}\n"
                    f"• Chat ID: {customer_chat_id}\n"
                    f"• 来源: telegram DM\n\n"
                    f"直接回复即可发送给用户。\n"
                    f"输入 /help 查看所有命令。"
                ),
            )
            logger.info("Created topic (thread=%s) for %s", thread_id, customer_chat_id)
            return thread_id
        except Exception as e:
            logger.error("Failed to create topic for %s: %s", customer_chat_id, e)
            raise

    # =========================================================================
    # DB Operations
    # =========================================================================

    async def _load_topic_from_db(self, customer_chat_id: str) -> int | None:
        try:
            async with self.db._db.execute(
                "SELECT thread_id FROM topic_mappings WHERE customer_chat_id = ?",
                (customer_chat_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else None
        except Exception:
            return None

    async def _load_customer_for_topic(self, thread_id: int) -> str | None:
        try:
            async with self.db._db.execute(
                "SELECT customer_chat_id FROM topic_mappings WHERE thread_id = ?",
                (thread_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    self._reverse_cache[thread_id] = row[0]
                    self._topic_cache[row[0]] = thread_id
                    return row[0]
        except Exception:
            pass
        return None

    async def _save_topic_mapping(self, customer_chat_id: str, thread_id: int) -> None:
        await self.db._db.execute(
            "INSERT OR REPLACE INTO topic_mappings (customer_chat_id, thread_id) VALUES (?, ?)",
            (customer_chat_id, thread_id),
        )
        await self.db._db.commit()

    async def _save_user_lang(self, customer_chat_id: str, lang: str) -> None:
        await self.db._db.execute(
            "UPDATE topic_mappings SET user_lang = ? WHERE customer_chat_id = ?",
            (lang, customer_chat_id),
        )
        await self.db._db.commit()

    async def _load_user_lang(self, customer_chat_id: str) -> str | None:
        try:
            async with self.db._db.execute(
                "SELECT user_lang FROM topic_mappings WHERE customer_chat_id = ?",
                (customer_chat_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row and row[0] else None
        except Exception:
            return None
