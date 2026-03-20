"""IdentityRouter — manages multiple adapter instances and routes messages by identity.

Enables multi-identity support: multiple adapters of the same channel type
(e.g., two Telegram accounts) each addressed by a unique identity_id.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator

from .adapter import ChannelAdapter
from .types import ChannelStatus, OutboundMessage, UnifiedMessage

logger = logging.getLogger(__name__)

# identity_id must be "channel:label" format
_IDENTITY_PATTERN = re.compile(r"^[a-zA-Z0-9_]+:[a-zA-Z0-9_]+$")


class IdentityRouter:
    """Manages multiple adapter instances and routes messages by identity.

    Example:
        router = IdentityRouter()
        router.register("telegram:personal", telegram_personal_adapter)
        router.register("telegram:work", telegram_work_adapter)

        # Route outbound by identity
        await router.send("telegram:personal", OutboundMessage(...))

        # Receive from all identities
        async for identity_id, msg in router.receive_all():
            print(f"Received on {identity_id}: {msg.content.text}")
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}
        self._defaults: dict[str, str] = {}  # channel -> identity_id

    @staticmethod
    def _validate_identity_id(identity_id: str) -> None:
        if not _IDENTITY_PATTERN.match(identity_id):
            raise ValueError(
                f"invalid identity_id {identity_id!r}: "
                f"must match 'channel:label' (alphanumeric/underscore only)"
            )

    @staticmethod
    def _channel_from_id(identity_id: str) -> str:
        return identity_id.split(":")[0]

    def register(self, identity_id: str, adapter: ChannelAdapter) -> "IdentityRouter":
        """Register an adapter with a unique identity_id.

        Args:
            identity_id: Unique identifier in "channel:label" format.
            adapter: The channel adapter instance.

        Returns:
            self (for chaining).

        Raises:
            ValueError: If identity_id format is invalid or already registered.
        """
        self._validate_identity_id(identity_id)
        if identity_id in self._adapters:
            raise ValueError(f"identity already registered: {identity_id}")
        self._adapters[identity_id] = adapter
        return self

    def unregister(self, identity_id: str) -> "IdentityRouter":
        """Remove an adapter by identity_id.

        Also removes the default for its channel if this was the default.

        Raises:
            KeyError: If identity_id is not registered.
        """
        if identity_id not in self._adapters:
            raise KeyError(f"identity not registered: {identity_id}")
        del self._adapters[identity_id]
        # Clean up default if this identity was the default
        channel = self._channel_from_id(identity_id)
        if self._defaults.get(channel) == identity_id:
            del self._defaults[channel]
        return self

    async def send(self, identity_id: str, msg: OutboundMessage) -> str | None:
        """Send a message via a specific identity.

        Raises:
            KeyError: If identity_id is not registered.
        """
        adapter = self._adapters.get(identity_id)
        if adapter is None:
            raise KeyError(f"identity not registered: {identity_id}")
        return await adapter.send(msg)

    def set_default(self, channel: str, identity_id: str) -> "IdentityRouter":
        """Set the default identity for a channel type.

        Raises:
            KeyError: If identity_id is not registered.
            ValueError: If identity_id doesn't belong to the given channel.
        """
        if identity_id not in self._adapters:
            raise KeyError(f"identity not registered: {identity_id}")
        if self._channel_from_id(identity_id) != channel:
            raise ValueError(
                f"identity {identity_id!r} does not belong to channel {channel!r}"
            )
        self._defaults[channel] = identity_id
        return self

    async def send_default(self, channel: str, msg: OutboundMessage) -> str | None:
        """Send a message via the default identity for a channel.

        Raises:
            KeyError: If no default is set for the channel.
        """
        identity_id = self._defaults.get(channel)
        if identity_id is None:
            raise KeyError(f"no default identity set for channel: {channel}")
        return await self.send(identity_id, msg)

    async def receive_all(self) -> AsyncIterator[tuple[str, UnifiedMessage]]:
        """Yield (identity_id, message) from all registered adapters concurrently."""
        queue: asyncio.Queue[tuple[str, UnifiedMessage]] = asyncio.Queue()
        done_event = asyncio.Event()
        active_count = len(self._adapters)

        if active_count == 0:
            return

        finished = 0

        async def _reader(iid: str, adapter: ChannelAdapter) -> None:
            nonlocal finished
            try:
                async for msg in adapter.receive():
                    await queue.put((iid, msg))
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("receive error for identity %s", iid)
            finally:
                finished += 1
                if finished >= active_count:
                    done_event.set()

        tasks = [
            asyncio.create_task(_reader(iid, adapter))
            for iid, adapter in self._adapters.items()
        ]

        try:
            while not done_event.is_set() or not queue.empty():
                get_task = asyncio.create_task(queue.get())
                done_task = asyncio.create_task(done_event.wait())
                finished_tasks, _ = await asyncio.wait(
                    [get_task, done_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if get_task in finished_tasks:
                    done_task.cancel()
                    yield get_task.result()
                else:
                    get_task.cancel()
                    # Drain remaining items
                    while not queue.empty():
                        yield queue.get_nowait()
                    break
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def connect_all(self) -> None:
        """Connect all registered adapters in parallel."""
        async def _connect_one(iid: str, adapter: ChannelAdapter) -> None:
            await adapter.connect()
            logger.info("connected identity %s", iid)

        results = await asyncio.gather(
            *(_connect_one(iid, a) for iid, a in self._adapters.items()),
            return_exceptions=True,
        )
        # Raise the first failure
        for iid, result in zip(self._adapters, results):
            if isinstance(result, Exception):
                logger.exception("failed to connect identity %s", iid)
                raise result

    async def disconnect_all(self) -> None:
        """Disconnect all registered adapters in parallel."""
        async def _disconnect_one(iid: str, adapter: ChannelAdapter) -> None:
            try:
                await adapter.disconnect()
                logger.info("disconnected identity %s", iid)
            except Exception:
                logger.exception("error disconnecting identity %s", iid)

        await asyncio.gather(
            *(_disconnect_one(iid, a) for iid, a in self._adapters.items())
        )

    def get_identities(self, channel: str | None = None) -> list[str]:
        """List registered identity IDs, optionally filtered by channel type."""
        if channel is None:
            return list(self._adapters.keys())
        return [
            iid for iid in self._adapters
            if self._channel_from_id(iid) == channel
        ]

    async def get_status_all(self) -> dict[str, ChannelStatus]:
        """Get status of all registered identities (parallel)."""
        async def _safe_status(iid: str, adapter: ChannelAdapter) -> tuple[str, ChannelStatus]:
            try:
                return iid, await adapter.get_status()
            except Exception as e:
                return iid, ChannelStatus(
                    connected=False,
                    channel=self._channel_from_id(iid),
                    error=str(e),
                )

        results = await asyncio.gather(
            *(_safe_status(iid, a) for iid, a in self._adapters.items())
        )
        return dict(results)
