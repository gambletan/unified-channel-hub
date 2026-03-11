"""Telegram adapter — bridges python-telegram-bot to UnifiedMessage.

Supports both polling (default) and webhook modes. Webhook mode uses
python-telegram-bot's built-in webhook support via an aiohttp server.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator, Literal

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

from ..adapter import ChannelAdapter
from ..types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)

logger = logging.getLogger(__name__)


class TelegramAdapter(ChannelAdapter):
    """
    Telegram channel adapter using python-telegram-bot.

    Supports two modes:
    - ``"polling"`` (default): long polling via ``getUpdates``
    - ``"webhook"``: starts an HTTP server and registers a webhook URL

    This is the ONLY file needed to add Telegram support.
    Compare with openclaw's 125-file Telegram implementation —
    all routing/session/middleware logic lives in the shared layer.
    """

    channel_id = "telegram"

    def __init__(
        self,
        token: str,
        *,
        parse_mode: str = "Markdown",
        mode: Literal["polling", "webhook"] = "polling",
        webhook_url: str | None = None,
        port: int = 8443,
        listen: str = "0.0.0.0",
        url_path: str = "/telegram-webhook",
    ) -> None:
        self._token = token
        self._parse_mode = parse_mode
        self._mode: Literal["polling", "webhook"] = mode
        self._webhook_url = webhook_url
        self._port = port
        self._listen = listen
        self._url_path = url_path
        self._app: Application | None = None  # type: ignore[type-arg]
        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._bot_username: str | None = None

    @property
    def mode(self) -> str:
        return self._mode

    async def connect(self) -> None:
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        self._app = (
            Application.builder()
            .token(self._token)
            .build()
        )

        # Register handlers that push to the unified queue
        self._app.add_handler(
            CommandHandler(
                self._app.bot.commands if hasattr(self._app.bot, "commands") else [],
                self._on_command,
            )
        )
        # Catch-all command handler (any /xxx)
        self._app.add_handler(MessageHandler(filters.COMMAND, self._on_command))
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        self._app.add_handler(MessageHandler(
            filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO
            | filters.VIDEO | filters.VIDEO_NOTE | filters.Sticker.ALL,
            self._on_media,
        ))
        self._app.add_handler(CallbackQueryHandler(self._on_callback))

        # Error handler to catch unhandled errors
        async def _error_handler(update, context):
            logger.error("telegram unhandled error: %s (update=%s)", context.error, update)
        self._app.add_error_handler(_error_handler)

        await self._app.initialize()
        await self._app.start()

        if self._mode == "webhook":
            await self._start_webhook()
        else:
            await self._app.updater.start_polling(drop_pending_updates=False)  # type: ignore[union-attr]

        me = await self._app.bot.get_me()
        self._bot_username = me.username
        self._connected = True
        logger.info("telegram connected (%s mode): @%s", self._mode, self._bot_username)

    async def disconnect(self) -> None:
        if self._app:
            if self._mode == "webhook":
                await self._stop_webhook()
            else:
                await self._app.updater.stop()  # type: ignore[union-attr]
            await self._app.stop()
            await self._app.shutdown()
        self._connected = False
        logger.info("telegram disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, msg: OutboundMessage) -> str | None:
        if not self._app:
            raise RuntimeError("telegram not connected")

        kwargs: dict = {"chat_id": int(msg.chat_id), "text": msg.text}
        if msg.thread_id:
            kwargs["message_thread_id"] = int(msg.thread_id)
        if msg.parse_mode:
            kwargs["parse_mode"] = msg.parse_mode
        elif self._parse_mode:
            kwargs["parse_mode"] = self._parse_mode
        if msg.reply_to_id:
            kwargs["reply_to_message_id"] = int(msg.reply_to_id)

        # Build inline keyboard if buttons provided
        if msg.buttons:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = [
                [
                    InlineKeyboardButton(
                        b.label,
                        callback_data=b.callback_data,
                        url=b.url,
                    )
                    for b in row
                ]
                for row in msg.buttons
            ]
            kwargs["reply_markup"] = InlineKeyboardMarkup(keyboard)

        try:
            sent = await self._app.bot.send_message(**kwargs)
        except Exception as e:
            # Markdown/HTML parse failure — retry as plain text
            if kwargs.get("parse_mode") and (
                "parse entities" in str(e).lower()
                or "can't parse" in str(e).lower()
            ):
                logger.debug("send failed with parse_mode=%s, retrying as plain text", kwargs["parse_mode"])
                kwargs.pop("parse_mode")
                sent = await self._app.bot.send_message(**kwargs)
            else:
                raise
        self._last_activity = datetime.now()
        return str(sent.message_id)

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="telegram",
            account_id=self._bot_username,
            last_activity=self._last_activity,
        )

    # -- Webhook management --

    async def _start_webhook(self) -> None:
        if not self._webhook_url:
            raise ValueError("webhook_url is required for webhook mode")

        full_url = self._webhook_url.rstrip("/") + self._url_path

        # Use python-telegram-bot's built-in webhook support
        await self._app.updater.start_webhook(  # type: ignore[union-attr]
            listen=self._listen,
            port=self._port,
            url_path=self._url_path,
            webhook_url=full_url,
            drop_pending_updates=True,
        )
        logger.info("telegram webhook started on %s:%d%s", self._listen, self._port, self._url_path)

    async def _stop_webhook(self) -> None:
        try:
            await self._app.updater.stop()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            await self._app.bot.delete_webhook()  # type: ignore[union-attr]
        except Exception:
            pass
        logger.info("telegram webhook stopped")

    # -- internal handlers: convert platform events to UnifiedMessage --

    async def _on_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.message.text:
            return
        text = update.message.text
        logger.info(
            "telegram _on_command: chat_id=%s thread=%s from=%s text=%s",
            update.message.chat_id,
            update.message.message_thread_id,
            update.message.from_user.id if update.message.from_user else "?",
            text[:50],
        )
        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0]  # strip /cmd@botname
        args = parts[1:]

        msg = self._build_message(
            update,
            MessageContent(
                type=ContentType.COMMAND,
                text=text,
                command=cmd,
                args=args,
            ),
        )
        await self._queue.put(msg)

    async def _on_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message or not update.message.text:
            return
        logger.info(
            "telegram _on_text: chat_id=%s thread=%s from=%s text=%s",
            update.message.chat_id,
            update.message.message_thread_id,
            update.message.from_user.id if update.message.from_user else "?",
            update.message.text[:50],
        )
        msg = self._build_message(
            update,
            MessageContent(type=ContentType.TEXT, text=update.message.text),
        )
        await self._queue.put(msg)

    async def _on_media(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.message:
            return
        m = update.message
        caption = m.caption or ""

        # Detect media type and get file_id for URL resolution
        file_id: str | None = None
        if m.voice:
            media_type = "voice"
            file_id = m.voice.file_id
        elif m.audio:
            media_type = "audio"
            file_id = m.audio.file_id
        elif m.video:
            media_type = "video"
            file_id = m.video.file_id
        elif m.video_note:
            media_type = "video_note"
            file_id = m.video_note.file_id
        elif m.sticker:
            media_type = "sticker"
            file_id = m.sticker.file_id
        elif m.photo:
            media_type = "photo"
            file_id = m.photo[-1].file_id  # largest resolution
        elif m.document:
            media_type = "document"
            file_id = m.document.file_id
        else:
            media_type = "unknown"

        # Resolve file URL so downstream middleware can fetch it
        media_url: str | None = None
        if file_id:
            try:
                f = await context.bot.get_file(file_id)
                media_url = f.file_path  # full HTTPS URL
            except Exception as exc:
                logger.warning("telegram: failed to get file URL for %s: %s", media_type, exc)

        logger.info(
            "telegram _on_media: chat_id=%s type=%s file_id=%s",
            m.chat_id, media_type, file_id and file_id[:20],
        )
        msg = self._build_message(
            update,
            MessageContent(
                type=ContentType.MEDIA,
                text=caption,
                media_type=media_type,
                media_url=media_url,
            ),
        )
        await self._queue.put(msg)

    async def _on_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            if not update.callback_query:
                return
            query = update.callback_query
            logger.info("telegram _on_callback: data=%s from=%s chat=%s",
                        query.data, query.from_user.id if query.from_user else "?",
                        query.message.chat_id if query.message else "?")

            sender = query.from_user
            msg = UnifiedMessage(
                id=str(query.id),
                channel="telegram",
                sender=Identity(
                    id=str(sender.id),
                    username=sender.username,
                    display_name=sender.full_name,
                ),
                content=MessageContent(
                    type=ContentType.CALLBACK,
                    text=query.data or "",
                    callback_data=query.data,
                ),
                chat_id=str(query.message.chat_id) if query.message else None,
                raw=update,
            )
            self._last_activity = datetime.now()
            await self._queue.put(msg)
        except Exception as e:
            logger.error("telegram _on_callback error: %s", e, exc_info=True)
            # Still try to answer so user doesn't see loading forever
            try:
                await update.callback_query.answer()
            except Exception:
                pass

    def _build_message(
        self, update: Update, content: MessageContent
    ) -> UnifiedMessage:
        tg_msg = update.message
        assert tg_msg is not None
        user = tg_msg.from_user
        assert user is not None

        self._last_activity = datetime.now()
        return UnifiedMessage(
            id=str(tg_msg.message_id),
            channel="telegram",
            sender=Identity(
                id=str(user.id),
                username=user.username,
                display_name=user.full_name,
            ),
            content=content,
            timestamp=tg_msg.date,
            chat_id=str(tg_msg.chat_id),
            thread_id=(
                str(tg_msg.message_thread_id) if tg_msg.message_thread_id else None
            ),
            reply_to_id=(
                str(tg_msg.reply_to_message.message_id)
                if tg_msg.reply_to_message
                else None
            ),
            raw=update,
        )
