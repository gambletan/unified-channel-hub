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
import base64
import io
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
        erp_client: Any | None = None,
        send_fn: Any | None = None,
    ):
        self.db = db
        self.tg = tg_adapter
        self.group_chat_id = group_chat_id
        self.router = router
        self.agent_ids = agent_ids or set()
        self.default_lang = default_lang
        self.reply_timeout = reply_timeout
        self.sensitive_words = sensitive_words or _DEFAULT_SENSITIVE
        self.erp = erp_client  # Optional ERPClient for user info lookups
        self._send_fn = send_fn  # ChannelManager.send() for multi-channel reply

        # Caches
        self._topic_cache: dict[str, int] = {}      # customer_chat_id → thread_id
        self._reverse_cache: dict[int, str] = {}     # thread_id → customer_chat_id
        self._customer_channel: dict[str, str] = {}  # customer_chat_id → channel name
        self._user_lang: dict[str, str] = {}         # customer_chat_id → lang code
        self._pending_timers: dict[str, asyncio.Task] = {}  # session → timeout task
        self._pending_ratings: dict[str, str] = {}         # customer_chat_id → ticket_id (awaiting text rating)

    @property
    def bot(self):
        return self.tg._app.bot

    async def _send_to_customer(self, customer_chat_id: str, text: str, **kwargs) -> None:
        """Send message to customer via their original channel."""
        channel = self._customer_channel.get(customer_chat_id, "telegram")
        if channel == "telegram":
            try:
                await self.bot.send_message(chat_id=int(customer_chat_id), text=text, **kwargs)
            except (ValueError, TypeError):
                logger.error("Invalid telegram chat_id (non-numeric): %s", customer_chat_id)
        elif self._send_fn:
            await self._send_fn(channel, customer_chat_id, text)
        else:
            logger.warning("No send_fn for channel %s, cannot send to %s", channel, customer_chat_id)

    async def process(self, msg: UnifiedMessage, next_handler: Handler) -> Any:
        chat_id = msg.chat_id or ""
        sender_id = msg.sender.id if msg.sender else ""
        text = msg.content.text or ""

        logger.info("TopicBridge.process: chat_id=%s sender=%s text=%s", chat_id, sender_id, text[:50])

        # Handle rating callbacks
        if msg.content.type == ContentType.CALLBACK and msg.content.callback_data:
            return await self._handle_callback(msg)

        if not text.strip() and msg.content.type != ContentType.MEDIA:
            return await next_handler(msg)

        # --- Cross-channel session link (e.g. Telegram user scanned webchat QR) ---
        link_session = (msg.metadata or {}).get("link_session_id")
        if link_session:
            return await self._handle_session_link(msg, chat_id, link_session)

        # --- Auth upgrade (guest → registered user) ---
        if (msg.metadata or {}).get("auth_upgrade") and chat_id:
            return await self._handle_auth_upgrade(msg, chat_id, next_handler)

        # --- Message from agent group ---
        if str(chat_id) == str(self.group_chat_id):
            return await self._handle_group_message(msg, sender_id, text)

        # --- Pending text-based rating (WhatsApp etc.) ---
        if chat_id in self._pending_ratings and text.strip() in ("1", "2", "3", "4", "5"):
            return await self._handle_text_rating(chat_id, int(text.strip()))

        # --- Customer DM ---
        return await self._handle_customer_dm(msg, next_handler, text)

    # =========================================================================
    # Cross-channel Session Link
    # =========================================================================

    async def _handle_session_link(self, msg: UnifiedMessage, chat_id: str, session_id: str) -> Any:
        """User scanned QR from webchat — link their new channel to the existing topic.

        e.g. User was chatting on webchat (session abc123), scans QR with Telegram,
        sends /start sid_abc123 → we link Telegram chat to the same topic.
        """
        channel = msg.channel or "telegram"
        customer_name = (
            msg.sender.display_name or msg.sender.username or chat_id
        ) if msg.sender else chat_id

        # Find existing topic by webchat session_id
        thread_id = self._topic_cache.get(session_id)
        if not thread_id:
            row = await self._load_topic_row(session_id)
            if row:
                thread_id = row["thread_id"]

        if not thread_id:
            # No existing topic for this session — treat as normal customer DM
            logger.info("Session link: no topic found for session %s, treating as new", session_id)
            return "Welcome! How can we help? 😊"

        # Link this new channel's chat_id to the same topic
        self._topic_cache[chat_id] = thread_id
        self._reverse_cache[thread_id] = chat_id
        self._customer_channel[chat_id] = channel

        # Save new mapping in DB (keep old session mapping too)
        await self._save_topic_mapping(chat_id, thread_id, channel)

        # Notify in topic
        _channel_labels = {"telegram": "Telegram", "webchat": "Web Chat", "whatsapp": "WhatsApp",
                           "discord": "Discord", "line": "LINE", "wechat": "WeChat", "slack": "Slack"}
        try:
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                text=(
                    f"🔗 用户切换渠道\n"
                    f"• 新渠道: {_channel_labels.get(channel, channel)}\n"
                    f"• Chat ID: {chat_id}\n"
                    f"• 用户: {customer_name}\n"
                    f"后续消息将通过 {_channel_labels.get(channel, channel)} 发送"
                ),
            )
            # Update topic title
            await self.bot.edit_forum_topic(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                name=f"👤 {customer_name}",
            )
        except Exception as e:
            logger.warning("Failed to notify session link: %s", e)

        logger.info("Session link: %s:%s → topic %s (from session %s)", channel, chat_id, thread_id, session_id)

        # Send welcome to the new channel
        try:
            await self._send_to_customer(chat_id, "已连接到您之前的会话，请继续对话 😊\nConnected to your previous session. Continue chatting!")
        except Exception:
            pass

        return None

    # =========================================================================
    # Auth Upgrade (guest → registered)
    # =========================================================================

    async def _handle_auth_upgrade(self, msg: UnifiedMessage, chat_id: str, next_handler: Handler) -> Any:
        """Guest user just logged in — migrate topic mapping and show ERP info.

        Migrates topic from old session key (anon_xxx) to stable user key (u_{userId}),
        so the same topic is found on future visits regardless of session.
        """
        user_id = msg.sender.id if msg.sender else ""
        user_name = (msg.sender.display_name or msg.sender.username or user_id) if msg.sender else user_id
        stable_key = f"u_{user_id}" if user_id else chat_id
        old_session = (msg.metadata or {}).get("previous_session_id", chat_id)

        # Find topic by old guest session_id
        thread_id = self._topic_cache.get(old_session) or self._topic_cache.get(chat_id)
        if not thread_id:
            # No existing topic, let it flow through as normal customer DM
            return await next_handler(msg)

        # Migrate topic mapping: old session → stable user key
        if stable_key != old_session:
            # Update DB: add new mapping with stable key, remove old session key
            await self._save_topic_mapping(stable_key, thread_id, msg.channel or "webchat")
            try:
                await self.db._db.execute(
                    "DELETE FROM topic_mappings WHERE customer_chat_id = ?", (old_session,),
                )
                await self.db._db.commit()
            except Exception:
                pass
            # Update caches — keep old session as alias so ongoing WS messages still route correctly
            self._topic_cache[stable_key] = thread_id
            self._topic_cache[old_session] = thread_id  # alias: old session → same thread
            if chat_id != old_session:
                self._topic_cache[chat_id] = thread_id
            self._reverse_cache[thread_id] = stable_key
            channel = self._customer_channel.get(old_session, msg.channel or "webchat")
            self._customer_channel[stable_key] = channel
            self._customer_channel[old_session] = channel
            logger.info("Migrated topic mapping: %s → %s (thread=%s)", old_session, stable_key, thread_id)

        # Update topic title with real name
        try:
            await self.bot.edit_forum_topic(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                name=f"👤 {user_name}",
            )
        except Exception as e:
            logger.warning("Failed to update topic title: %s", e)

        # Fetch and display ERP user info
        erp_info = await self._fetch_erp_user_info(user_id)
        notify = f"🔗 用户已登录: {user_name} (ID: {user_id})"
        if erp_info:
            notify += f"\n\n{erp_info}"

        try:
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                text=notify,
            )
        except Exception as e:
            logger.warning("Failed to send auth upgrade notice: %s", e)

        # Pass through to IdentityMiddleware for binding storage
        return await next_handler(msg)

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
                    text=f"⭐ Customer rating: {'⭐' * rating} ({rating}/5)",
                )
            except Exception:
                pass

        # Answer callback and update message
        if msg.raw:
            try:
                query = msg.raw.callback_query
                if query:
                    await query.answer(f"Thanks! {'⭐' * rating}")
                    await query.edit_message_text(f"Thank you for your feedback! {'⭐' * rating} ({rating}/5)")
            except Exception as e:
                logger.warning("Failed to answer/edit callback: %s", e)

        logger.info("Rating %d for ticket %s", rating, ticket_id)
        return None

    async def _handle_text_rating(self, customer_chat_id: str, rating: int) -> None:
        """Handle text-based rating reply (for WhatsApp and other non-button channels)."""
        ticket_id = self._pending_ratings.pop(customer_chat_id)

        from ..models import SatisfactionRating
        await self.db.add_rating(SatisfactionRating(
            ticket_id=ticket_id, rating=rating,
        ))
        await self.db.log_event("rated", ticket_id=ticket_id)

        # Notify in topic
        topic_id = self._topic_cache.get(customer_chat_id)
        if topic_id:
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"⭐ Customer rating: {'⭐' * rating} ({rating}/5)",
                )
            except Exception:
                pass

        await self._send_to_customer(
            customer_chat_id,
            f"Thank you for your feedback! {'⭐' * rating}\nFeel free to message us anytime. 👋",
        )
        logger.info("Text rating %d for ticket %s", rating, ticket_id)

    # =========================================================================
    # Customer DM → Topic
    # =========================================================================

    async def _handle_customer_dm(
        self, msg: UnifiedMessage, next_handler: Handler, text: str
    ) -> Any:
        chat_id = msg.chat_id or ""
        channel = msg.channel or "telegram"
        customer_name = (
            msg.sender.display_name or msg.sender.username or chat_id
            if msg.sender else chat_id
        )

        # Remember which channel this customer came from
        self._customer_channel[chat_id] = channel

        topic_id = await self._get_or_create_topic(chat_id, customer_name, channel)

        # Mark for downstream middleware to skip agent detection
        if not hasattr(msg, "metadata") or msg.metadata is None:
            msg.metadata = {}
        msg.metadata["topic_bridge"] = True

        # If this chat has a stable key (u_xxx), inject platform_user_id
        # so AI knows the user is logged in even if IdentityMiddleware can't find the binding
        stable_key = self._reverse_cache.get(topic_id, "")
        if stable_key.startswith("u_"):
            msg.metadata["platform_user_id"] = stable_key[2:]

        # ── Step 1: Forward message to topic IMMEDIATELY (no blocking) ──
        is_media = msg.content.type == ContentType.MEDIA
        if is_media:
            await self._forward_media_to_topic(msg, topic_id, text, text)
        else:
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"👤 {text}",
                )
            except Exception as e:
                logger.error("Failed to forward to topic %s: %s", topic_id, e)

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

        # ── Step 2: Language detection + translation (async, non-blocking) ──
        lang = await self._detect_language(text)
        prev_lang = self._user_lang.get(chat_id, self.default_lang)
        if lang != prev_lang:
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

        # Append translation to topic if needed (as follow-up message)
        if lang != self.default_lang and self.router.get_backend("translate"):
            asyncio.create_task(self._send_translation(topic_id, text, self.default_lang, lang))

        # Inject detected language so AI replies in the correct language
        msg.metadata["user_lang"] = lang

        # ── Step 3: Run AI pipeline ──
        result = await next_handler(msg)

        # ── Step 4: Forward AI reply to topic ──
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

            # Append translation of AI reply if user is non-default language
            if lang != self.default_lang and self.router.get_backend("translate"):
                asyncio.create_task(self._send_ai_translation(topic_id, result, self.default_lang))

        return result

    async def _send_translation(self, topic_id: int, text: str, target_lang: str, source_lang: str) -> None:
        """Send translation as a follow-up message in the topic (fire-and-forget)."""
        try:
            translated = await self._translate(text, target_lang, source_lang)
            if translated and translated != text:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"🌐 _{translated}_",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.warning("Translation follow-up failed: %s", e)

    async def _send_ai_translation(self, topic_id: int, text: str, target_lang: str) -> None:
        """Send translation of AI reply as a follow-up (fire-and-forget)."""
        try:
            reply_lang = await self._detect_language(text)
            if reply_lang != target_lang:
                translated = await self._translate(text, target_lang, reply_lang)
                if translated and translated != text:
                    await self.bot.send_message(
                        chat_id=self.group_chat_id,
                        message_thread_id=topic_id,
                        text=f"📝 _{translated}_",
                        parse_mode="Markdown",
                    )
        except Exception as e:
            logger.warning("AI translation follow-up failed: %s", e)

    # =========================================================================
    # Media Forwarding (all channels)
    # =========================================================================

    async def _forward_media_to_topic(
        self, msg: UnifiedMessage, topic_id: int, text: str, display_text: str,
    ) -> None:
        """Forward media from any channel to the agent group topic.

        - Telegram: native forward (preserves original message)
        - Webchat/other: decode base64 media_url and send via bot API
        """
        media_type = msg.content.media_type or "unknown"
        media_label = f"👤 [{media_type}]"
        if text:
            media_label += f" {display_text}"

        # --- Telegram native forward ---
        tg_msg = None
        if msg.raw and hasattr(msg.raw, "message"):
            tg_msg = msg.raw.message
        if tg_msg and hasattr(tg_msg, "forward"):
            try:
                await tg_msg.forward(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                )
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=media_label,
                )
                return
            except Exception as e:
                logger.warning("Telegram forward failed, falling back to send: %s", e)

        # --- Non-Telegram (webchat, etc.): decode media and send via bot ---
        media_url = msg.content.media_url
        if not media_url:
            # No media data, just send label
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=topic_id,
                text=media_label,
            )
            return

        try:
            file_bytes = self._decode_media_url(media_url)
            buf = io.BytesIO(file_bytes)
            caption = text or None
            kwargs = {
                "chat_id": self.group_chat_id,
                "message_thread_id": topic_id,
                "caption": caption,
            }

            if media_type in ("voice", "audio"):
                buf.name = "voice.ogg"
                await self.bot.send_voice(**kwargs, voice=buf)
            elif media_type == "video":
                buf.name = "video.mp4"
                await self.bot.send_video(**kwargs, video=buf)
            elif media_type in ("photo", "image"):
                buf.name = "photo.jpg"
                await self.bot.send_photo(**kwargs, photo=buf)
            elif media_type == "sticker":
                buf.name = "sticker.webp"
                await self.bot.send_sticker(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    sticker=buf,
                )
            else:
                # document / unknown — send as file
                buf.name = f"file.{media_type}" if media_type != "unknown" else "file.bin"
                await self.bot.send_document(**kwargs, document=buf)

            # Also send text label
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=topic_id,
                text=media_label,
            )
        except Exception as e:
            logger.error("Failed to send media to topic %s: %s", topic_id, e, exc_info=True)
            # Fallback: at least send the label
            try:
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=topic_id,
                    text=f"{media_label} (media send failed)",
                )
            except Exception:
                pass

    @staticmethod
    def _decode_media_url(media_url: str) -> bytes:
        """Decode base64 data URI or raw base64 string to bytes."""
        if media_url.startswith("data:"):
            # data:audio/webm;base64,AAAA...
            _, encoded = media_url.split(",", 1)
        else:
            encoded = media_url
        return base64.b64decode(encoded)

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

        # Forward to customer (via their original channel)
        customer_channel = self._customer_channel.get(customer_chat_id, "telegram")
        try:
            await self._send_to_customer(customer_chat_id, send_text)
            # Store agent message via DB layer
            ticket = await self.db.find_ticket_by_chat(customer_channel, customer_chat_id)
            if ticket:
                await self.db.add_message(TicketMessage(
                    ticket_id=ticket.id,
                    role="agent",
                    sender_id=sender_id,
                    content=text,
                    channel=customer_channel,
                    from_id=sender_id,
                    to_id=ticket.customer_id,
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
        logger.info("agent_command: cmd=%s thread=%s customer=%s cache=%s", cmd, thread_id, customer_chat_id, dict(self._reverse_cache))

        if cmd == "close":
            # Close ticket
            customer_channel = self._customer_channel.get(customer_chat_id, "telegram") if customer_chat_id else "telegram"
            if customer_chat_id:
                ticket = await self.db.find_ticket_by_chat(customer_channel, customer_chat_id)
                if ticket and ticket.status != TicketStatus.CLOSED:
                    await self.db.update_ticket_status(ticket.id, TicketStatus.CLOSED)
                    if ticket.assigned_agent_id:
                        await self.db.update_agent_load(ticket.assigned_agent_id, -1)
                    await self.db.log_event("closed", ticket_id=ticket.id)
                self._cancel_timer(customer_chat_id)
                # Notify customer with rating
                try:
                    if customer_channel == "telegram" and ticket:
                        # Telegram supports inline keyboard buttons
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
                            text="Your session has ended. Please rate our service:",
                            reply_markup=keyboard,
                        )
                    else:
                        # Other channels: plain text close message with rating prompt
                        rating_text = (
                            "Your session has ended. Please rate our service:\n\n"
                            "Reply with a number:\n"
                            "1 ⭐ — Poor\n"
                            "2 ⭐⭐ — Fair\n"
                            "3 ⭐⭐⭐ — Good\n"
                            "4 ⭐⭐⭐⭐ — Very Good\n"
                            "5 ⭐⭐⭐⭐⭐ — Excellent\n\n"
                            "Or just send a new message to start a new conversation. 👋"
                        )
                        if ticket:
                            self._pending_ratings[customer_chat_id] = ticket.id
                        await self._send_to_customer(customer_chat_id, rating_text)
                except Exception:
                    try:
                        await self._send_to_customer(
                            customer_chat_id,
                            "Your session has ended. Feel free to message us anytime. 👋",
                        )
                    except Exception:
                        pass
            # Mark topic as closed in DB
            if customer_chat_id:
                await self._set_topic_closed(customer_chat_id, True)

            # Close topic in Telegram
            try:
                await self.bot.close_forum_topic(
                    chat_id=self.group_chat_id,
                    message_thread_id=thread_id,
                )
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=thread_id,
                    text="🔒 Session closed",
                )
            except Exception as e:
                logger.error("Failed to close topic: %s", e)

        elif cmd == "history":
            # Show ALL message history across all tickets for this customer
            if customer_chat_id:
                cc = self._customer_channel.get(customer_chat_id, "telegram")
                all_tickets = await self.db.find_all_tickets_by_chat(cc, customer_chat_id)
                if all_tickets:
                    lines = []
                    for ticket in all_tickets:
                        status_icon = {"open": "🟢", "closed": "🔒", "resolved": "✅", "escalated": "🔴", "assigned": "💬"}.get(ticket.status.value, "•")
                        ts = ticket.created_at.strftime("%m-%d %H:%M")
                        lines.append(f"\n{'─' * 20}")
                        lines.append(f"{status_icon} 工单 {ticket.id[:8]} [{ts}] {ticket.subject or ''}")
                        messages = await self.db.get_messages(ticket.id)
                        for m in messages[-10:]:
                            icon = {"customer": "👤", "ai": "🤖", "agent": "💬"}.get(m.role, "•")
                            mts = m.created_at.strftime("%m-%d %H:%M") if m.created_at else ""
                            lines.append(f"  {icon} [{mts}] {m.content[:80]}")
                    text_out = "\n".join(lines)
                    # Telegram message limit is 4096 chars
                    if len(text_out) > 4000:
                        text_out = text_out[-4000:]
                        text_out = "...(截断)\n" + text_out
                    try:
                        await self.bot.send_message(
                            chat_id=self.group_chat_id,
                            message_thread_id=thread_id,
                            text=text_out,
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

        elif cmd == "user":
            # Show ERP user info (strip u_ prefix for stable keys)
            if customer_chat_id:
                lookup_id = customer_chat_id[2:] if customer_chat_id.startswith("u_") else customer_chat_id
                erp_info = await self._fetch_erp_user_info(lookup_id)
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=thread_id,
                    text=erp_info or "❌ 未找到用户信息",
                )

        elif cmd == "orders":
            # Show customer orders
            if customer_chat_id:
                orders_text = await self._fetch_orders(customer_chat_id, args)
                await self.bot.send_message(
                    chat_id=self.group_chat_id,
                    message_thread_id=thread_id,
                    text=orders_text,
                )

        elif cmd == "help":
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                text=(
                    "📋 可用命令:\n"
                    "/close — 关闭此会话\n"
                    "/history — 查看全部历史消息\n"
                    "/user — 查看用户信息(ERP)\n"
                    "/orders [订单号] — 查看用户订单\n"
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

        # Latin script — short text defaults to English (LLM unreliable on few words)
        stripped = text.strip()
        if len(stripped) < 10 and re.match(r'^[a-zA-Z\s!?.,:;]+$', stripped):
            return "en"

        # Latin script — use LLM for longer text
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

    async def _get_or_create_topic(self, customer_chat_id: str, customer_name: str, channel: str = "telegram") -> int:
        if customer_chat_id in self._topic_cache:
            logger.info("topic cache hit: %s → thread=%s", customer_chat_id, self._topic_cache[customer_chat_id])
            return self._topic_cache[customer_chat_id]

        # Check DB for existing topic mapping (persists across restarts)
        row = await self._load_topic_row(customer_chat_id)
        if row:
            thread_id = row["thread_id"]
            is_closed = bool(row["closed"])
            self._topic_cache[customer_chat_id] = thread_id
            self._reverse_cache[thread_id] = customer_chat_id
            # Restore channel (update if user switched channels)
            saved_channel = row.get("channel", "telegram")
            if channel != saved_channel:
                await self._save_topic_channel(customer_chat_id, channel)
            self._customer_channel[customer_chat_id] = channel
            # Load saved language
            lang = row["user_lang"]
            if lang:
                self._user_lang[customer_chat_id] = lang

            # Reopen if topic was closed
            if is_closed:
                try:
                    await self.bot.reopen_forum_topic(
                        chat_id=self.group_chat_id,
                        message_thread_id=thread_id,
                    )
                    await self._set_topic_closed(customer_chat_id, False)
                    # Show previous conversation summary + ERP user info
                    summary = await self._get_customer_summary(customer_chat_id)
                    erp_info = await self._fetch_erp_user_info(customer_chat_id)
                    reopen_text = f"🔓 用户 {customer_name} 发起新会话\n{summary}"
                    if erp_info:
                        reopen_text += f"\n\n{erp_info}"
                    await self.bot.send_message(
                        chat_id=self.group_chat_id,
                        message_thread_id=thread_id,
                        text=reopen_text,
                    )
                    logger.info("Reopened topic (thread=%s) for %s", thread_id, customer_chat_id)
                except Exception as e:
                    logger.warning("Failed to reopen topic %s: %s", thread_id, e)
            return thread_id

        try:
            # Determine topic icon color by user type:
            #   Guest (anonymous) → blue, Registered → yellow, VIP → red
            _TOPIC_COLOR_GUEST = 7322096       # blue
            _TOPIC_COLOR_REGISTERED = 16766590  # yellow
            _TOPIC_COLOR_VIP = 16478047         # red
            icon_color = _TOPIC_COLOR_GUEST
            erp_info = None
            if not self._is_guest(customer_chat_id):
                erp_info = await self._fetch_erp_user_info(customer_chat_id)
                if erp_info:
                    # ERP found → registered; check user_level for VIP
                    icon_color = _TOPIC_COLOR_REGISTERED
                    user_info = await self._get_erp_user_obj(customer_chat_id)
                    if user_info and user_info.user_level >= 1:
                        icon_color = _TOPIC_COLOR_VIP

            topic = await self.bot.create_forum_topic(
                chat_id=self.group_chat_id,
                name=f"👤 {customer_name}",
                icon_color=icon_color,
            )
            thread_id = topic.message_thread_id
            self._topic_cache[customer_chat_id] = thread_id
            self._reverse_cache[thread_id] = customer_chat_id

            await self._save_topic_mapping(customer_chat_id, thread_id, channel)

            # Build welcome message with optional ERP user info
            _channel_labels = {"telegram": "Telegram", "webchat": "Web Chat", "whatsapp": "WhatsApp", "discord": "Discord", "line": "LINE", "wechat": "WeChat", "slack": "Slack"}
            welcome = (
                f"📋 新会话\n"
                f"• 用户: {customer_name}\n"
                f"• Chat ID: {customer_chat_id}\n"
                f"• 来源: {_channel_labels.get(channel, channel)}\n"
            )
            if not erp_info and not self._is_guest(customer_chat_id):
                erp_info = await self._fetch_erp_user_info(customer_chat_id)
            if erp_info:
                welcome += f"\n{erp_info}\n"
            welcome += "\n直接回复即可发送给用户。\n输入 /help 查看所有命令。"

            await self.bot.send_message(
                chat_id=self.group_chat_id,
                message_thread_id=thread_id,
                text=welcome,
            )
            logger.info("Created topic (thread=%s) for %s", thread_id, customer_chat_id)
            return thread_id
        except Exception as e:
            logger.error("Failed to create topic for %s: %s", customer_chat_id, e)
            raise

    # =========================================================================
    # DB Operations
    # =========================================================================

    async def _load_topic_row(self, customer_chat_id: str) -> dict | None:
        """Load full topic mapping row (thread_id, user_lang, closed, channel)."""
        try:
            async with self.db._db.execute(
                "SELECT thread_id, user_lang, closed, channel FROM topic_mappings WHERE customer_chat_id = ?",
                (customer_chat_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {"thread_id": int(row[0]), "user_lang": row[1], "closed": row[2] or 0, "channel": row[3] or "telegram"}
                return None
        except Exception:
            return None

    async def _fetch_orders(self, customer_chat_id: str, args: list[str]) -> str:
        """Fetch and format orders for display in agent topic."""
        if not self.erp:
            return "❌ ERP 未配置"
        if not args and self._is_guest(customer_chat_id):
            return "👤 游客用户，无订单记录"
        try:
            # If arg provided, treat as order ID search
            if args:
                result = await self.erp.get_orders(order_id=args[0])
            else:
                # Try by thirdId (Telegram ID) first, then userId via binding
                result = await self.erp.get_orders(third_id=customer_chat_id, page_size=5)
                if not result.orders:
                    cc = self._customer_channel.get(customer_chat_id, "telegram")
                    binding = await self.db.get_binding_by_chat(cc, customer_chat_id)
                    if binding:
                        result = await self.erp.get_orders(user_id=binding.platform_user_id, page_size=5)
            text = result.summary_for_agent()
            # Telegram 4096 char limit
            if len(text) > 4000:
                text = text[:4000] + "\n...(截断)"
            return text
        except Exception as e:
            logger.warning("Order lookup failed for %s: %s", customer_chat_id, e)
            return f"❌ 订单查询失败: {e}"

    def _is_guest(self, customer_chat_id: str) -> bool:
        """Check if customer is an anonymous/guest user (no ERP account)."""
        return customer_chat_id.startswith("guest_") or customer_chat_id.startswith("anon_")

    async def _get_erp_user_obj(self, customer_chat_id: str):
        """Fetch raw ERP UserInfo object (or None)."""
        if not self.erp or self._is_guest(customer_chat_id):
            return None
        try:
            cc = self._customer_channel.get(customer_chat_id, "telegram")
            binding = await self.db.get_binding_by_chat(cc, customer_chat_id)
            user_info = None
            if binding:
                user_info = await self.erp.get_user_info(binding.platform_user_id)
            if not user_info:
                user_info = await self.erp.get_user_info(customer_chat_id)
            return user_info
        except Exception as e:
            logger.warning("ERP user obj lookup failed for %s: %s", customer_chat_id, e)
            return None

    async def _fetch_erp_user_info(self, customer_chat_id: str) -> str | None:
        """Fetch and format ERP user info for display in agent topic."""
        user_info = await self._get_erp_user_obj(customer_chat_id)
        if user_info:
            return user_info.summary_for_agent()
        return None

    async def _get_customer_summary(self, customer_chat_id: str) -> str:
        """Build a brief summary of previous conversations for this customer."""
        cc = self._customer_channel.get(customer_chat_id, "telegram")
        all_tickets = await self.db.find_all_tickets_by_chat(cc, customer_chat_id)
        if not all_tickets:
            return "📋 首次联系"
        total = len(all_tickets)
        last = all_tickets[-1]
        last_msgs = await self.db.get_messages(last.id)
        last_topic = last.subject or (last_msgs[0].content[:40] if last_msgs else "")
        lines = [f"📋 历史会话: {total} 次"]
        lines.append(f"最近话题: {last_topic}")
        if last.resolved_at:
            lines.append(f"上次结束: {last.resolved_at.strftime('%m-%d %H:%M')}")
        return "\n".join(lines)

    async def _set_topic_closed(self, customer_chat_id: str, closed: bool) -> None:
        await self.db._db.execute(
            "UPDATE topic_mappings SET closed = ? WHERE customer_chat_id = ?",
            (1 if closed else 0, customer_chat_id),
        )
        await self.db._db.commit()

    async def _load_customer_for_topic(self, thread_id: int) -> str | None:
        try:
            async with self.db._db.execute(
                "SELECT customer_chat_id, channel FROM topic_mappings WHERE thread_id = ?",
                (thread_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    cid = row[0]
                    self._reverse_cache[thread_id] = cid
                    self._topic_cache[cid] = thread_id
                    if row[1]:
                        self._customer_channel[cid] = row[1]
                    return cid
        except Exception:
            pass
        return None

    async def _save_topic_mapping(self, customer_chat_id: str, thread_id: int, channel: str = "telegram") -> None:
        await self.db._db.execute(
            """INSERT INTO topic_mappings (customer_chat_id, thread_id, channel)
               VALUES (?, ?, ?)
               ON CONFLICT(customer_chat_id) DO UPDATE SET
               thread_id=excluded.thread_id, channel=excluded.channel""",
            (customer_chat_id, thread_id, channel),
        )
        await self.db._db.commit()

    async def _save_topic_channel(self, customer_chat_id: str, channel: str) -> None:
        await self.db._db.execute(
            "UPDATE topic_mappings SET channel = ? WHERE customer_chat_id = ?",
            (channel, customer_chat_id),
        )
        await self.db._db.commit()

    async def _save_user_lang(self, customer_chat_id: str, lang: str) -> None:
        await self.db._db.execute(
            "UPDATE topic_mappings SET user_lang = ? WHERE customer_chat_id = ?",
            (lang, customer_chat_id),
        )
        await self.db._db.commit()

