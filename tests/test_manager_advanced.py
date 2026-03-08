"""Advanced ChannelManager tests — multi-channel, outbound message, broadcast, edge cases."""

from __future__ import annotations

import asyncio

import pytest

from unified_channel.adapter import ChannelAdapter
from unified_channel.manager import ChannelManager
from unified_channel.middleware import AccessMiddleware, CommandMiddleware, Middleware
from unified_channel.types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


class MockAdapter(ChannelAdapter):
    def __init__(self, name: str = "mock"):
        self.channel_id = name
        self._connected = False
        self._inbound: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self.sent: list[OutboundMessage] = []

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def receive(self):
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._inbound.get(), timeout=0.1)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, msg: OutboundMessage) -> str | None:
        self.sent.append(msg)
        return str(len(self.sent))

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(connected=self._connected, channel=self.channel_id)

    async def inject(self, text: str, *, command: str | None = None,
                     args: list[str] | None = None, sender_id: str = "user1"):
        ct = ContentType.COMMAND if command else ContentType.TEXT
        msg = UnifiedMessage(
            id=f"msg-{self._inbound.qsize()}",
            channel=self.channel_id,
            sender=Identity(id=sender_id, username="tester"),
            content=MessageContent(type=ct, text=text, command=command, args=args or []),
            chat_id="chat1",
        )
        await self._inbound.put(msg)


@pytest.mark.asyncio
async def test_multi_channel():
    """Commands work across multiple channels."""
    tg = MockAdapter("telegram")
    dc = MockAdapter("discord")
    manager = ChannelManager()
    manager.add_channel(tg)
    manager.add_channel(dc)

    cmds = CommandMiddleware()
    manager.add_middleware(cmds)

    @cmds.command("whoami")
    async def whoami(msg):
        return f"You're on {msg.channel}"

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await tg.inject("/whoami", command="whoami")
    await dc.inject("/whoami", command="whoami")
    await asyncio.sleep(0.3)

    assert len(tg.sent) == 1
    assert tg.sent[0].text == "You're on telegram"
    assert len(dc.sent) == 1
    assert dc.sent[0].text == "You're on discord"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_outbound_message_return():
    """Handler can return OutboundMessage for full control."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)

    cmds = CommandMiddleware()
    manager.add_middleware(cmds)

    @cmds.command("fancy")
    async def fancy(msg):
        return OutboundMessage(
            chat_id=msg.chat_id,
            text="*bold*",
            parse_mode="Markdown",
        )

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("/fancy", command="fancy")
    await asyncio.sleep(0.2)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "*bold*"
    assert adapter.sent[0].parse_mode == "Markdown"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_send_direct():
    """manager.send() pushes to specific channel."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)

    await adapter.connect()
    result = await manager.send("mock", "chat1", "hello direct")
    assert result == "1"
    assert adapter.sent[0].text == "hello direct"


@pytest.mark.asyncio
async def test_send_unknown_channel():
    manager = ChannelManager()
    with pytest.raises(ValueError, match="channel not registered"):
        await manager.send("nonexistent", "chat1", "hello")


@pytest.mark.asyncio
async def test_broadcast():
    tg = MockAdapter("telegram")
    dc = MockAdapter("discord")
    manager = ChannelManager()
    manager.add_channel(tg)
    manager.add_channel(dc)

    await tg.connect()
    await dc.connect()

    await manager.broadcast("deploy done", {"telegram": "tg_chat", "discord": "dc_chat"})
    assert len(tg.sent) == 1
    assert tg.sent[0].chat_id == "tg_chat"
    assert len(dc.sent) == 1
    assert dc.sent[0].chat_id == "dc_chat"


@pytest.mark.asyncio
async def test_middleware_chain_order():
    """First-added middleware runs first."""
    call_order = []

    class TrackingMiddleware(Middleware):
        def __init__(self, name: str):
            self.name = name

        async def process(self, msg, next_handler):
            call_order.append(self.name)
            return await next_handler(msg)

    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)
    manager.add_middleware(TrackingMiddleware("first"))
    manager.add_middleware(TrackingMiddleware("second"))
    manager.add_middleware(TrackingMiddleware("third"))

    @manager.on_message
    async def echo(msg):
        call_order.append("handler")
        return "done"

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("test")
    await asyncio.sleep(0.2)

    assert call_order == ["first", "second", "third", "handler"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_middleware_short_circuit():
    """Middleware can short-circuit the chain."""

    class BlockMiddleware(Middleware):
        async def process(self, msg, next_handler):
            return "blocked"

    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)
    manager.add_middleware(BlockMiddleware())

    @manager.on_message
    async def should_not_reach(msg):
        return "should not reach"

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("anything")
    await asyncio.sleep(0.2)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "blocked"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_no_fallback_no_reply():
    """No fallback handler = no reply for non-command messages."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("hello")
    await asyncio.sleep(0.2)

    assert len(adapter.sent) == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_handler_returns_none():
    """Handler returning None = no reply."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)

    @manager.on_message
    async def silent(msg):
        return None

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("hello")
    await asyncio.sleep(0.2)

    assert len(adapter.sent) == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_command_with_args_parsing():
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)

    cmds = CommandMiddleware()
    manager.add_middleware(cmds)

    @cmds.command("deploy")
    async def deploy(msg):
        env = msg.content.args[0] if msg.content.args else "prod"
        force = "--force" in msg.content.args
        return f"deploy {env} force={force}"

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("/deploy staging --force", command="deploy",
                          args=["staging", "--force"])
    await asyncio.sleep(0.2)

    assert adapter.sent[0].text == "deploy staging force=True"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_no_channels_raises():
    manager = ChannelManager()
    with pytest.raises(RuntimeError, match="no channels registered"):
        await manager.run()


@pytest.mark.asyncio
async def test_get_status_multi():
    tg = MockAdapter("telegram")
    dc = MockAdapter("discord")
    manager = ChannelManager()
    manager.add_channel(tg)
    manager.add_channel(dc)

    await tg.connect()
    statuses = await manager.get_status()
    assert statuses["telegram"].connected is True
    assert statuses["discord"].connected is False


@pytest.mark.asyncio
async def test_access_then_command():
    """AccessMiddleware before CommandMiddleware blocks unauthorized commands."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)
    manager.add_middleware(AccessMiddleware(allowed_user_ids={"admin"}))

    cmds = CommandMiddleware()
    manager.add_middleware(cmds)

    @cmds.command("secret")
    async def secret(msg):
        return "top secret data"

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    # user1 is not in allowlist
    await adapter.inject("/secret", command="secret", sender_id="user1")
    await asyncio.sleep(0.2)
    assert len(adapter.sent) == 0

    # admin is allowed
    await adapter.inject("/secret", command="secret", sender_id="admin")
    await asyncio.sleep(0.2)
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "top secret data"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_fluent_api():
    """add_channel and add_middleware return self for chaining."""
    adapter = MockAdapter()
    manager = ChannelManager()
    result = manager.add_channel(adapter)
    assert result is manager
    result = manager.add_middleware(AccessMiddleware())
    assert result is manager
