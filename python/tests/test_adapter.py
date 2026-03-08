"""Tests for adapter base class and protocol compliance."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from unified_channel.adapter import ChannelAdapter
from unified_channel.types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


class DummyAdapter(ChannelAdapter):
    """Minimal concrete adapter for testing the base class."""

    channel_id = "dummy"

    def __init__(self):
        self._connected = False
        self._sent: list[OutboundMessage] = []

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        yield UnifiedMessage(
            id="1", channel="dummy",
            sender=Identity(id="u1"),
            content=MessageContent(type=ContentType.TEXT, text="test"),
            chat_id="c1",
        )

    async def send(self, msg: OutboundMessage) -> str | None:
        self._sent.append(msg)
        return "sent-1"

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(connected=self._connected, channel="dummy")


@pytest.mark.asyncio
async def test_adapter_connect_disconnect():
    adapter = DummyAdapter()
    assert adapter.channel_id == "dummy"

    await adapter.connect()
    status = await adapter.get_status()
    assert status.connected is True

    await adapter.disconnect()
    status = await adapter.get_status()
    assert status.connected is False


@pytest.mark.asyncio
async def test_adapter_receive():
    adapter = DummyAdapter()
    messages = []
    async for msg in adapter.receive():
        messages.append(msg)
    assert len(messages) == 1
    assert messages[0].content.text == "test"
    assert messages[0].channel == "dummy"


@pytest.mark.asyncio
async def test_adapter_send():
    adapter = DummyAdapter()
    out = OutboundMessage(chat_id="c1", text="reply")
    result = await adapter.send(out)
    assert result == "sent-1"
    assert len(adapter._sent) == 1
    assert adapter._sent[0].text == "reply"


@pytest.mark.asyncio
async def test_adapter_run_forever():
    """run_forever should connect, run, then disconnect on cancel."""
    adapter = DummyAdapter()
    task = asyncio.create_task(adapter.run_forever())
    await asyncio.sleep(0.05)
    assert adapter._connected is True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert adapter._connected is False


def test_adapter_is_abstract():
    """Cannot instantiate ChannelAdapter directly."""
    with pytest.raises(TypeError):
        ChannelAdapter()  # type: ignore[abstract]
