"""Cross-Channel Relay Middleware — forward messages between channels.

Example usage:
    relay = RelayMiddleware()
    relay.add_rule("telegram", "slack", channel_id="general")
    relay.add_rule("slack", "email", channel_id="team@company.com", transform=summarize)
    relay.add_rule("*", "telegram", channel_id="123456", filter_fn=is_urgent)

    manager.add_middleware(relay)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .middleware import Handler, Middleware
from .types import OutboundMessage, UnifiedMessage

logger = logging.getLogger(__name__)


@dataclass
class RelayRule:
    """A rule defining how messages are forwarded between channels."""

    source: str  # source channel_id or "*" for all
    target: str  # target channel_id
    target_chat_id: str  # chat_id in the target channel (e.g., Slack channel, email address)
    filter_fn: Callable[[UnifiedMessage], bool] | None = None  # optional filter
    transform: Callable[[UnifiedMessage], str] | None = None  # optional text transform
    include_sender: bool = True  # prepend sender info
    bidirectional: bool = False  # relay in both directions


class RelayMiddleware(Middleware):
    """Forward messages between channels based on configurable rules.

    Messages are relayed AFTER the handler processes them, so the original
    channel's response is not affected.
    """

    def __init__(self) -> None:
        self._rules: list[RelayRule] = []
        self._manager = None  # set by ChannelManager when middleware is added

    def set_manager(self, manager) -> None:
        """Called by ChannelManager to give relay access to send across channels."""
        self._manager = manager

    def add_rule(
        self,
        source: str,
        target: str,
        *,
        target_chat_id: str,
        filter_fn: Callable[[UnifiedMessage], bool] | None = None,
        transform: Callable[[UnifiedMessage], str] | None = None,
        include_sender: bool = True,
        bidirectional: bool = False,
    ) -> RelayMiddleware:
        """Add a relay rule. Returns self for chaining."""
        rule = RelayRule(
            source=source,
            target=target,
            target_chat_id=target_chat_id,
            filter_fn=filter_fn,
            transform=transform,
            include_sender=include_sender,
            bidirectional=bidirectional,
        )
        self._rules.append(rule)

        if bidirectional:
            reverse = RelayRule(
                source=target,
                target=source,
                target_chat_id=target_chat_id,
                filter_fn=filter_fn,
                transform=transform,
                include_sender=include_sender,
                bidirectional=False,
            )
            self._rules.append(reverse)

        return self

    def add_broadcast(
        self,
        source: str,
        targets: dict[str, str],
        *,
        filter_fn: Callable[[UnifiedMessage], bool] | None = None,
        transform: Callable[[UnifiedMessage], str] | None = None,
    ) -> RelayMiddleware:
        """Broadcast from one source to multiple targets.

        Args:
            source: Source channel_id
            targets: Dict of {channel_id: chat_id} pairs
        """
        for target_channel, chat_id in targets.items():
            self.add_rule(
                source,
                target_channel,
                target_chat_id=chat_id,
                filter_fn=filter_fn,
                transform=transform,
            )
        return self

    async def process(
        self, msg: UnifiedMessage, next_handler: Handler
    ) -> str | OutboundMessage | None:
        # Let the original handler process first
        result = await next_handler(msg)

        # Then relay to matching targets
        matching_rules = [
            r for r in self._rules
            if (r.source == "*" or r.source == msg.channel)
            and (r.filter_fn is None or r.filter_fn(msg))
        ]

        if matching_rules and self._manager:
            for rule in matching_rules:
                try:
                    await self._relay(msg, rule)
                except Exception as e:
                    logger.warning(
                        "relay failed: %s → %s: %s", msg.channel, rule.target, e
                    )

        return result

    async def _relay(self, msg: UnifiedMessage, rule: RelayRule) -> None:
        if not self._manager:
            return

        # Build relay text
        if rule.transform:
            text = rule.transform(msg)
        else:
            text = msg.content.text or ""

        if rule.include_sender:
            sender_name = msg.sender.display_name or msg.sender.username or msg.sender.id
            text = f"[{msg.channel}/{sender_name}] {text}"

        outbound = OutboundMessage(
            chat_id=rule.target_chat_id,
            text=text,
            metadata={"relayed_from": msg.channel, "original_id": msg.id},
        )

        adapter = self._manager.get_adapter(rule.target)
        if adapter:
            await adapter.send(outbound)
            logger.debug("relayed: %s → %s (%s)", msg.channel, rule.target, rule.target_chat_id)
        else:
            logger.warning("relay target adapter not found: %s", rule.target)
