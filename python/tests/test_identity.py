"""Tests for IdentityRouter — multi-identity adapter routing."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from unified_channel.adapter import ChannelAdapter
from unified_channel.identity import IdentityRouter
from unified_channel.types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


def _make_msg(
    channel: str = "telegram",
    text: str = "hello",
    msg_id: str = "m1",
    chat_id: str = "chat1",
) -> UnifiedMessage:
    return UnifiedMessage(
        id=msg_id,
        channel=channel,
        sender=Identity(id="user1", username="user1"),
        content=MessageContent(type=ContentType.TEXT, text=text),
        chat_id=chat_id,
    )


class FakeAdapter(ChannelAdapter):
    """In-memory adapter for testing."""

    def __init__(self, channel_id: str = "telegram", messages: list[UnifiedMessage] | None = None):
        self.channel_id = channel_id
        self.sent: list[OutboundMessage] = []
        self._messages = messages or []
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        for msg in self._messages:
            yield msg

    async def send(self, msg: OutboundMessage) -> str | None:
        self.sent.append(msg)
        return f"sent-{len(self.sent)}"

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(connected=self.connected, channel=self.channel_id)


class FailingAdapter(FakeAdapter):
    """Adapter whose get_status raises."""

    async def get_status(self) -> ChannelStatus:
        raise ConnectionError("status check failed")


# ── Registration ─────────────────────────────────────────────────────


def test_register_and_list():
    router = IdentityRouter()
    adapter = FakeAdapter("telegram")
    router.register("telegram:personal", adapter)
    assert router.get_identities() == ["telegram:personal"]


def test_register_duplicate_raises():
    router = IdentityRouter()
    router.register("telegram:personal", FakeAdapter("telegram"))
    with pytest.raises(ValueError, match="already registered"):
        router.register("telegram:personal", FakeAdapter("telegram"))


def test_register_invalid_format_raises():
    router = IdentityRouter()
    with pytest.raises(ValueError, match="invalid identity_id"):
        router.register("no-colon", FakeAdapter("telegram"))


def test_register_invalid_chars_raises():
    router = IdentityRouter()
    with pytest.raises(ValueError, match="invalid identity_id"):
        router.register("telegram:my-personal", FakeAdapter("telegram"))


def test_unregister():
    router = IdentityRouter()
    router.register("telegram:work", FakeAdapter("telegram"))
    router.unregister("telegram:work")
    assert router.get_identities() == []


def test_unregister_unknown_raises():
    router = IdentityRouter()
    with pytest.raises(KeyError, match="not registered"):
        router.unregister("telegram:nope")


@pytest.mark.asyncio
async def test_unregister_clears_default():
    router = IdentityRouter()
    router.register("telegram:main", FakeAdapter("telegram"))
    router.set_default("telegram", "telegram:main")
    router.unregister("telegram:main")
    # Default should be cleared
    with pytest.raises(KeyError, match="no default"):
        await router.send_default("telegram", OutboundMessage(chat_id="c", text="hi"))


# ── Send ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_to_specific_identity():
    router = IdentityRouter()
    adapter = FakeAdapter("telegram")
    router.register("telegram:personal", adapter)

    msg = OutboundMessage(chat_id="123", text="hello")
    result = await router.send("telegram:personal", msg)

    assert result == "sent-1"
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "hello"


@pytest.mark.asyncio
async def test_send_unknown_identity_raises():
    router = IdentityRouter()
    with pytest.raises(KeyError, match="not registered"):
        await router.send("telegram:ghost", OutboundMessage(chat_id="c", text="hi"))


@pytest.mark.asyncio
async def test_send_default():
    router = IdentityRouter()
    personal = FakeAdapter("telegram")
    work = FakeAdapter("telegram")
    router.register("telegram:personal", personal)
    router.register("telegram:work", work)
    router.set_default("telegram", "telegram:work")

    msg = OutboundMessage(chat_id="123", text="via default")
    await router.send_default("telegram", msg)

    assert len(work.sent) == 1
    assert len(personal.sent) == 0
    assert work.sent[0].text == "via default"


@pytest.mark.asyncio
async def test_send_default_no_default_raises():
    router = IdentityRouter()
    router.register("telegram:personal", FakeAdapter("telegram"))
    with pytest.raises(KeyError, match="no default"):
        await router.send_default("telegram", OutboundMessage(chat_id="c", text="hi"))


def test_set_default_unknown_identity_raises():
    router = IdentityRouter()
    with pytest.raises(KeyError, match="not registered"):
        router.set_default("telegram", "telegram:nope")


def test_set_default_wrong_channel_raises():
    router = IdentityRouter()
    router.register("slack:team", FakeAdapter("slack"))
    with pytest.raises(ValueError, match="does not belong"):
        router.set_default("telegram", "slack:team")


# ── Receive ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_receive_all_from_multiple_identities():
    msg1 = _make_msg(channel="telegram", text="from personal", msg_id="m1")
    msg2 = _make_msg(channel="telegram", text="from work", msg_id="m2")

    router = IdentityRouter()
    router.register("telegram:personal", FakeAdapter("telegram", messages=[msg1]))
    router.register("telegram:work", FakeAdapter("telegram", messages=[msg2]))

    received: list[tuple[str, UnifiedMessage]] = []
    async for identity_id, msg in router.receive_all():
        received.append((identity_id, msg))

    assert len(received) == 2
    ids = {iid for iid, _ in received}
    assert ids == {"telegram:personal", "telegram:work"}
    texts = {m.content.text for _, m in received}
    assert texts == {"from personal", "from work"}


@pytest.mark.asyncio
async def test_receive_all_empty():
    router = IdentityRouter()
    received = []
    async for item in router.receive_all():
        received.append(item)
    assert received == []


# ── get_identities filtering ─────────────────────────────────────────


def test_get_identities_filter_by_channel():
    router = IdentityRouter()
    router.register("telegram:a", FakeAdapter("telegram"))
    router.register("telegram:b", FakeAdapter("telegram"))
    router.register("slack:main", FakeAdapter("slack"))

    assert sorted(router.get_identities("telegram")) == ["telegram:a", "telegram:b"]
    assert router.get_identities("slack") == ["slack:main"]
    assert router.get_identities("discord") == []


def test_get_identities_all():
    router = IdentityRouter()
    router.register("telegram:a", FakeAdapter("telegram"))
    router.register("slack:b", FakeAdapter("slack"))
    assert sorted(router.get_identities()) == ["slack:b", "telegram:a"]


# ── connect_all / disconnect_all ─────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_all():
    router = IdentityRouter()
    a1 = FakeAdapter("telegram")
    a2 = FakeAdapter("slack")
    router.register("telegram:main", a1)
    router.register("slack:main", a2)

    await router.connect_all()

    assert a1.connected
    assert a2.connected


@pytest.mark.asyncio
async def test_disconnect_all():
    router = IdentityRouter()
    a1 = FakeAdapter("telegram")
    a2 = FakeAdapter("slack")
    router.register("telegram:main", a1)
    router.register("slack:main", a2)

    await router.disconnect_all()

    assert a1.disconnected
    assert a2.disconnected


# ── Status ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_status_all():
    router = IdentityRouter()
    a1 = FakeAdapter("telegram")
    a1.connected = True
    a2 = FakeAdapter("slack")
    router.register("telegram:main", a1)
    router.register("slack:main", a2)

    statuses = await router.get_status_all()

    assert statuses["telegram:main"].connected is True
    assert statuses["slack:main"].connected is False


@pytest.mark.asyncio
async def test_get_status_all_handles_errors():
    router = IdentityRouter()
    router.register("telegram:broken", FailingAdapter("telegram"))

    statuses = await router.get_status_all()

    assert statuses["telegram:broken"].connected is False
    assert "status check failed" in (statuses["telegram:broken"].error or "")


# ── Multiple same-channel identities ────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_same_channel_send_independently():
    router = IdentityRouter()
    personal = FakeAdapter("telegram")
    work = FakeAdapter("telegram")
    router.register("telegram:personal", personal)
    router.register("telegram:work", work)

    await router.send("telegram:personal", OutboundMessage(chat_id="a", text="p"))
    await router.send("telegram:work", OutboundMessage(chat_id="b", text="w"))

    assert len(personal.sent) == 1
    assert personal.sent[0].text == "p"
    assert len(work.sent) == 1
    assert work.sent[0].text == "w"


# ── Chaining API ────────────────────────────────────────────────────


def test_register_returns_self():
    router = IdentityRouter()
    result = router.register("telegram:a", FakeAdapter("telegram"))
    assert result is router


def test_fluent_chaining():
    router = (
        IdentityRouter()
        .register("telegram:a", FakeAdapter("telegram"))
        .register("slack:b", FakeAdapter("slack"))
        .set_default("telegram", "telegram:a")
    )
    assert len(router.get_identities()) == 2
