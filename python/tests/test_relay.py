"""Tests for cross-channel relay middleware."""

from __future__ import annotations

import pytest
import pytest_asyncio

from unified_channel.relay import RelayMiddleware, RelayRule
from unified_channel.types import (
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


async def _noop_handler(msg: UnifiedMessage):
    return None


async def _ok_handler(msg: UnifiedMessage):
    return "ok"


def _make_msg(
    channel: str = "telegram",
    text: str = "hello",
    sender_id: str = "user1",
    chat_id: str = "chat1",
    msg_id: str = "m1",
) -> UnifiedMessage:
    return UnifiedMessage(
        id=msg_id,
        channel=channel,
        sender=Identity(id=sender_id, username=sender_id, display_name=f"User {sender_id}"),
        content=MessageContent(type=ContentType.TEXT, text=text),
        chat_id=chat_id,
        raw={},
    )


class FakeAdapter:
    """Mock adapter that records sent messages."""

    def __init__(self, channel_id: str = "slack"):
        self.channel_id = channel_id
        self.sent: list[OutboundMessage] = []

    async def send(self, msg: OutboundMessage) -> str | None:
        self.sent.append(msg)
        return None


class FakeManager:
    """Mock manager with adapters."""

    def __init__(self):
        self.adapters: dict[str, FakeAdapter] = {}

    def add_adapter(self, adapter: FakeAdapter) -> None:
        self.adapters[adapter.channel_id] = adapter

    def get_adapter(self, channel_id: str):
        return self.adapters.get(channel_id)


@pytest.mark.asyncio
async def test_basic_relay():
    """Messages from telegram should relay to slack."""
    relay = RelayMiddleware()
    relay.add_rule("telegram", "slack", target_chat_id="general")

    slack = FakeAdapter("slack")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    relay.set_manager(mgr)

    msg = _make_msg(channel="telegram", text="test message")
    result = await relay.process(msg, _ok_handler)
    assert result == "ok"
    assert len(slack.sent) == 1
    assert "test message" in slack.sent[0].text
    assert slack.sent[0].chat_id == "general"


@pytest.mark.asyncio
async def test_relay_includes_sender():
    """Relayed message should include sender info by default."""
    relay = RelayMiddleware()
    relay.add_rule("telegram", "slack", target_chat_id="ch1")

    slack = FakeAdapter("slack")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    relay.set_manager(mgr)

    msg = _make_msg(channel="telegram", text="hello world", sender_id="alice")
    await relay.process(msg, _noop_handler)

    assert "[telegram/User alice]" in slack.sent[0].text
    assert "hello world" in slack.sent[0].text


@pytest.mark.asyncio
async def test_relay_without_sender():
    """When include_sender=False, no sender prefix."""
    relay = RelayMiddleware()
    relay.add_rule("telegram", "slack", target_chat_id="ch1", include_sender=False)

    slack = FakeAdapter("slack")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    relay.set_manager(mgr)

    msg = _make_msg(text="raw text")
    await relay.process(msg, _noop_handler)

    assert slack.sent[0].text == "raw text"


@pytest.mark.asyncio
async def test_relay_filter():
    """Only messages passing filter should be relayed."""
    relay = RelayMiddleware()
    relay.add_rule(
        "telegram",
        "email",
        target_chat_id="admin@example.com",
        filter_fn=lambda m: "urgent" in (m.content.text or "").lower(),
    )

    email_adapter = FakeAdapter("email")
    mgr = FakeManager()
    mgr.add_adapter(email_adapter)
    relay.set_manager(mgr)

    # Normal message — not relayed
    msg1 = _make_msg(text="just chatting")
    await relay.process(msg1, _noop_handler)
    assert len(email_adapter.sent) == 0

    # Urgent message — relayed
    msg2 = _make_msg(text="URGENT: server down", msg_id="m2")
    await relay.process(msg2, _noop_handler)
    assert len(email_adapter.sent) == 1
    assert "server down" in email_adapter.sent[0].text


@pytest.mark.asyncio
async def test_relay_transform():
    """Custom transform function should modify relayed text."""
    relay = RelayMiddleware()
    relay.add_rule(
        "telegram",
        "slack",
        target_chat_id="alerts",
        transform=lambda m: f"ALERT: {m.content.text}",
        include_sender=False,
    )

    slack = FakeAdapter("slack")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    relay.set_manager(mgr)

    msg = _make_msg(text="CPU at 95%")
    await relay.process(msg, _noop_handler)

    assert slack.sent[0].text == "ALERT: CPU at 95%"


@pytest.mark.asyncio
async def test_wildcard_source():
    """Wildcard '*' source matches all channels."""
    relay = RelayMiddleware()
    relay.add_rule("*", "slack", target_chat_id="firehose")

    slack = FakeAdapter("slack")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    relay.set_manager(mgr)

    for ch in ["telegram", "discord", "email", "twilio_sms"]:
        msg = _make_msg(channel=ch, text=f"from {ch}", msg_id=ch)
        await relay.process(msg, _noop_handler)

    assert len(slack.sent) == 4
    assert "telegram" in slack.sent[0].text
    assert "discord" in slack.sent[1].text


@pytest.mark.asyncio
async def test_broadcast():
    """Broadcast sends to multiple targets."""
    relay = RelayMiddleware()
    relay.add_broadcast(
        "telegram",
        {"slack": "general", "email": "team@co.com", "discord": "12345"},
    )

    slack = FakeAdapter("slack")
    email_a = FakeAdapter("email")
    discord_a = FakeAdapter("discord")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    mgr.add_adapter(email_a)
    mgr.add_adapter(discord_a)
    relay.set_manager(mgr)

    msg = _make_msg(text="broadcast test")
    await relay.process(msg, _noop_handler)

    assert len(slack.sent) == 1
    assert len(email_a.sent) == 1
    assert len(discord_a.sent) == 1
    assert slack.sent[0].chat_id == "general"
    assert email_a.sent[0].chat_id == "team@co.com"


@pytest.mark.asyncio
async def test_bidirectional():
    """Bidirectional rule creates both directions."""
    relay = RelayMiddleware()
    relay.add_rule("telegram", "slack", target_chat_id="ch1", bidirectional=True)

    slack = FakeAdapter("slack")
    telegram = FakeAdapter("telegram")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    mgr.add_adapter(telegram)
    relay.set_manager(mgr)

    # Telegram → Slack
    msg1 = _make_msg(channel="telegram", text="from tg")
    await relay.process(msg1, _noop_handler)
    assert len(slack.sent) == 1

    # Slack → Telegram
    msg2 = _make_msg(channel="slack", text="from slack", msg_id="m2")
    await relay.process(msg2, _noop_handler)
    assert len(telegram.sent) == 1


@pytest.mark.asyncio
async def test_no_relay_for_unmatched_source():
    """Messages from unmatched channels are not relayed."""
    relay = RelayMiddleware()
    relay.add_rule("telegram", "slack", target_chat_id="ch1")

    slack = FakeAdapter("slack")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    relay.set_manager(mgr)

    msg = _make_msg(channel="discord", text="should not relay")
    await relay.process(msg, _noop_handler)

    assert len(slack.sent) == 0


@pytest.mark.asyncio
async def test_relay_metadata():
    """Relayed messages include metadata about the source."""
    relay = RelayMiddleware()
    relay.add_rule("telegram", "slack", target_chat_id="ch1")

    slack = FakeAdapter("slack")
    mgr = FakeManager()
    mgr.add_adapter(slack)
    relay.set_manager(mgr)

    msg = _make_msg(channel="telegram", msg_id="orig123")
    await relay.process(msg, _noop_handler)

    meta = slack.sent[0].metadata
    assert meta["relayed_from"] == "telegram"
    assert meta["original_id"] == "orig123"


@pytest.mark.asyncio
async def test_relay_missing_adapter():
    """Relay to missing adapter should not crash."""
    relay = RelayMiddleware()
    relay.add_rule("telegram", "nonexistent", target_chat_id="ch1")

    mgr = FakeManager()  # no adapters
    relay.set_manager(mgr)

    msg = _make_msg()
    # Should not raise
    result = await relay.process(msg, _ok_handler)
    assert result == "ok"


@pytest.mark.asyncio
async def test_relay_adapter_error():
    """Relay send failure should not break the handler chain."""
    relay = RelayMiddleware()
    relay.add_rule("telegram", "slack", target_chat_id="ch1")

    class BrokenAdapter:
        channel_id = "slack"
        async def send(self, msg):
            raise ConnectionError("network down")

    mgr = FakeManager()
    mgr.adapters["slack"] = BrokenAdapter()
    relay.set_manager(mgr)

    msg = _make_msg()
    result = await relay.process(msg, _ok_handler)
    assert result == "ok"


@pytest.mark.asyncio
async def test_chaining_api():
    """add_rule returns self for fluent chaining."""
    relay = (
        RelayMiddleware()
        .add_rule("telegram", "slack", target_chat_id="ch1")
        .add_rule("slack", "email", target_chat_id="a@b.com")
    )
    assert len(relay._rules) == 2
