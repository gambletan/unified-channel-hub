"""ChannelManager — ties adapters + middleware together into a running system."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .adapter import ChannelAdapter
from .middleware import CommandMiddleware, Handler, Middleware
from .types import ContentType, MessageContent, OutboundMessage, UnifiedMessage

logger = logging.getLogger(__name__)


class ChannelManager:
    """
    Central manager: register channels + middleware, start all, route messages.

    Usage:
        manager = ChannelManager()
        manager.add_channel(TelegramAdapter(token="..."))
        manager.add_middleware(AccessMiddleware(allowed_user_ids={"123"}))

        cmds = CommandMiddleware()
        @cmds.command("status")
        async def status(msg): return "all good"
        manager.add_middleware(cmds)

        await manager.run()
    """

    def __init__(
        self,
        *,
        broadcast_concurrency: int = 10,
        status_cache_ttl: float = 5.0,
    ) -> None:
        self._channels: dict[str, ChannelAdapter] = {}
        self._middlewares: list[Middleware] = []
        self._fallback_handler: Handler | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._cached_pipeline: Handler | None = None
        self.broadcast_concurrency = broadcast_concurrency
        self.status_cache_ttl = status_cache_ttl
        self._status_cache: dict[str, Any] | None = None
        self._status_cache_time: float = 0.0

    def add_channel(self, adapter: ChannelAdapter) -> "ChannelManager":
        self._channels[adapter.channel_id] = adapter
        return self

    def add_middleware(self, mw: Middleware) -> "ChannelManager":
        self._middlewares.append(mw)
        self._cached_pipeline = None  # invalidate cached chain
        return self

    def on_message(self, handler: Handler) -> Handler:
        """Set fallback handler for non-command messages (decorator or direct)."""
        self._fallback_handler = handler
        self._cached_pipeline = None  # invalidate cached chain
        return handler

    async def send(
        self,
        channel: str,
        chat_id: str,
        text: str,
        *,
        reply_to_id: str | None = None,
        thread_id: str | None = None,
        parse_mode: str | None = None,
    ) -> str | None:
        """Send a message to a specific channel + chat."""
        adapter = self._channels.get(channel)
        if not adapter:
            raise ValueError(f"channel not registered: {channel}")
        return await adapter.send(
            OutboundMessage(
                chat_id=chat_id,
                text=text,
                reply_to_id=reply_to_id,
                thread_id=thread_id,
                parse_mode=parse_mode,
            )
        )

    async def broadcast(self, text: str, chat_ids: dict[str, str]) -> None:
        """Send to multiple channels in batches to avoid connection storms."""
        entries = list(chat_ids.items())
        for i in range(0, len(entries), self.broadcast_concurrency):
            batch = entries[i : i + self.broadcast_concurrency]
            await asyncio.gather(
                *(self.send(ch, cid, text) for ch, cid in batch),
                return_exceptions=True,
            )

    async def get_status(self) -> dict[str, Any]:
        """Get status of all registered channels (parallel, cached with TTL)."""
        now = time.monotonic()
        if self._status_cache is not None and (now - self._status_cache_time) < self.status_cache_ttl:
            return self._status_cache

        async def _safe_status(cid: str, adapter: ChannelAdapter) -> tuple[str, Any]:
            try:
                return cid, await adapter.get_status()
            except Exception as e:
                return cid, {"connected": False, "error": str(e)}

        results = await asyncio.gather(
            *[_safe_status(cid, a) for cid, a in self._channels.items()]
        )
        self._status_cache = dict(results)
        self._status_cache_time = now
        return self._status_cache

    async def run(self) -> None:
        """Start all channels and process messages."""
        if not self._channels:
            raise RuntimeError("no channels registered")

        # Connect all adapters in parallel
        await asyncio.gather(
            *(adapter.connect() for adapter in self._channels.values())
        )
        for adapter in self._channels.values():
            task = asyncio.create_task(
                self._consume(adapter), name=f"channel:{adapter.channel_id}"
            )
            self._tasks.append(task)

        logger.info(
            "unified-channel started: channels=%s",
            list(self._channels.keys()),
        )
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Stop all channels gracefully."""
        for task in self._tasks:
            task.cancel()
        for adapter in self._channels.values():
            try:
                await adapter.disconnect()
            except Exception:
                logger.exception("error disconnecting %s", adapter.channel_id)
        self._tasks.clear()
        logger.info("unified-channel shut down")

    async def _consume(self, adapter: ChannelAdapter) -> None:
        """Read from one channel adapter and process through middleware."""
        try:
            async for msg in adapter.receive():
                try:
                    reply = await self._run_pipeline(msg)
                    if reply and msg.chat_id:
                        out = self._to_outbound(reply, msg)
                        logger.info(
                            "outbound chat_id=%s thread_id=%s",
                            out.chat_id, out.thread_id,
                        )
                        await adapter.send(out)
                except Exception:
                    logger.exception(
                        "error processing message id=%s channel=%s",
                        msg.id,
                        msg.channel,
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("channel %s consumer crashed", adapter.channel_id)

    def _build_pipeline(self) -> Handler:
        """Build the middleware chain once and cache it."""

        async def fallback(m: UnifiedMessage) -> str | OutboundMessage | None:
            if self._fallback_handler:
                return await self._fallback_handler(m)
            return None

        handler: Handler = fallback
        for mw in reversed(self._middlewares):

            def make_next(
                current_mw: Middleware, next_h: Handler
            ) -> Handler:
                async def wrapped(m: UnifiedMessage) -> str | OutboundMessage | None:
                    return await current_mw.process(m, next_h)

                return wrapped

            handler = make_next(mw, handler)

        return handler

    async def _run_pipeline(
        self, msg: UnifiedMessage
    ) -> str | OutboundMessage | None:
        """Run message through cached middleware chain."""
        if self._cached_pipeline is None:
            self._cached_pipeline = self._build_pipeline()
        return await self._cached_pipeline(msg)

    @staticmethod
    def _to_outbound(reply: str | OutboundMessage, orig: UnifiedMessage) -> OutboundMessage:
        if isinstance(reply, OutboundMessage):
            if not reply.chat_id:
                reply.chat_id = orig.chat_id or ""
            return reply
        return OutboundMessage(
            chat_id=orig.chat_id or "",
            text=reply,
            reply_to_id=orig.id,
            thread_id=orig.thread_id,
        )
