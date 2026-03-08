"""End-to-end integration tests — full pipeline from adapter to reply."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from unified_channel.adapter import ChannelAdapter
from unified_channel.bridge import ServiceBridge
from unified_channel.manager import ChannelManager
from unified_channel.memory import ConversationMemory, InMemoryStore
from unified_channel.middleware import AccessMiddleware, CommandMiddleware, Middleware
from unified_channel.rich import RichReply
from unified_channel.streaming import StreamingMiddleware, StreamingReply
from unified_channel.types import (
    Button,
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


class MockAdapter(ChannelAdapter):
    """Full mock adapter for integration tests."""

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
                     args: list[str] | None = None, sender_id: str = "user1",
                     chat_id: str = "chat1"):
        ct = ContentType.COMMAND if command else ContentType.TEXT
        msg = UnifiedMessage(
            id=f"msg-{self._inbound.qsize()}",
            channel=self.channel_id,
            sender=Identity(id=sender_id, username="tester"),
            content=MessageContent(type=ct, text=text, command=command, args=args or []),
            chat_id=chat_id,
        )
        await self._inbound.put(msg)


@pytest.mark.asyncio
async def test_full_pipeline_access_command_memory():
    """Full pipeline: adapter -> access -> memory -> command -> reply."""
    adapter = MockAdapter("telegram")
    manager = ChannelManager()
    manager.add_channel(adapter)

    # Add access middleware
    manager.add_middleware(AccessMiddleware(allowed_user_ids={"admin"}))

    # Add memory
    store = InMemoryStore()
    manager.add_middleware(ConversationMemory(store=store, max_turns=20))

    # Add commands
    cmds = CommandMiddleware()
    manager.add_middleware(cmds)

    @cmds.command("status")
    async def status(msg):
        history = msg.metadata.get("history", [])
        return f"ok, history_len={len(history)}"

    @manager.on_message
    async def fallback(msg):
        return f"echo: {msg.content.text}"

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    # Blocked user gets no reply
    await adapter.inject("hi", sender_id="hacker")
    await asyncio.sleep(0.2)
    assert len(adapter.sent) == 0

    # Admin gets echo reply
    await adapter.inject("hello", sender_id="admin")
    await asyncio.sleep(0.2)
    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "echo: hello"

    # Admin runs command - memory should have previous turn
    await adapter.inject("/status", command="status", sender_id="admin")
    await asyncio.sleep(0.2)
    assert len(adapter.sent) == 2
    assert "history_len=2" in adapter.sent[1].text  # user+assistant from previous

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_bridge_full_pipeline():
    """ServiceBridge: expose -> trigger command -> verify response."""
    adapter = MockAdapter("test")
    manager = ChannelManager()
    manager.add_channel(adapter)
    bridge = ServiceBridge(manager)

    async def deploy(args: list[str], msg: UnifiedMessage) -> str:
        env = args[0] if args else "prod"
        return f"deployed to {env} by {msg.sender.username}"

    bridge.expose("deploy", deploy, description="Deploy app", params=["env"])

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("/deploy staging", command="deploy", args=["staging"])
    await asyncio.sleep(0.2)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "deployed to staging by tester"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_broadcast_across_three_adapters():
    """Broadcast sends to all three adapters."""
    tg = MockAdapter("telegram")
    dc = MockAdapter("discord")
    sl = MockAdapter("slack")
    manager = ChannelManager()
    manager.add_channel(tg).add_channel(dc).add_channel(sl)

    await tg.connect()
    await dc.connect()
    await sl.connect()

    await manager.broadcast(
        "System maintenance in 5 minutes",
        {"telegram": "tg_general", "discord": "dc_general", "slack": "sl_general"},
    )

    assert len(tg.sent) == 1
    assert tg.sent[0].chat_id == "tg_general"
    assert len(dc.sent) == 1
    assert dc.sent[0].chat_id == "dc_general"
    assert len(sl.sent) == 1
    assert sl.sent[0].chat_id == "sl_general"

    for adapter in [tg, dc, sl]:
        assert adapter.sent[0].text == "System maintenance in 5 minutes"


@pytest.mark.asyncio
async def test_rich_reply_through_pipeline():
    """RichReply is converted correctly when sent through pipeline."""
    adapter = MockAdapter("telegram")
    manager = ChannelManager()
    manager.add_channel(adapter)

    cmds = CommandMiddleware()
    manager.add_middleware(cmds)

    @cmds.command("info")
    async def info(msg):
        reply = RichReply("Service Info")
        reply.add_table(["Metric", "Value"], [["CPU", "42%"], ["RAM", "60%"]])
        reply.add_buttons([[Button(label="Refresh", callback_data="refresh")]])
        return reply.to_outbound("telegram")

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("/info", command="info")
    await asyncio.sleep(0.2)

    assert len(adapter.sent) == 1
    sent = adapter.sent[0]
    assert "Service Info" in sent.text
    assert sent.parse_mode == "Markdown"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_streaming_through_full_pipeline():
    """StreamingReply works through full middleware pipeline."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)

    streaming_mw = StreamingMiddleware(typing_interval=0.5, chunk_delay=0)
    manager.add_middleware(streaming_mw)

    @manager.on_message
    async def handler(msg):
        async def chunks() -> AsyncIterator[str]:
            yield "Hello"
            yield ", "
            yield "World!"
        return StreamingReply(chunks())

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("trigger stream")
    await asyncio.sleep(0.3)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "Hello, World!"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_middleware_chain_with_memory_and_access():
    """Access + Memory + Command middleware all interact correctly."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)

    store = InMemoryStore()
    manager.add_middleware(AccessMiddleware(allowed_user_ids={"user1"}))
    manager.add_middleware(ConversationMemory(store=store))

    cmds = CommandMiddleware()
    manager.add_middleware(cmds)

    @cmds.command("history")
    async def show_history(msg):
        h = msg.metadata.get("history", [])
        return f"entries={len(h)}"

    @manager.on_message
    async def echo(msg):
        return f"echo: {msg.content.text}"

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    # First message from allowed user
    await adapter.inject("hello", sender_id="user1")
    await asyncio.sleep(0.2)

    # Check history command
    await adapter.inject("/history", command="history", sender_id="user1")
    await asyncio.sleep(0.2)

    assert len(adapter.sent) == 2
    assert adapter.sent[0].text == "echo: hello"
    assert "entries=2" in adapter.sent[1].text  # user msg + echo reply

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_multi_adapter_with_bridge_commands():
    """Bridge commands work across multiple adapters."""
    tg = MockAdapter("telegram")
    dc = MockAdapter("discord")
    manager = ChannelManager()
    manager.add_channel(tg).add_channel(dc)
    bridge = ServiceBridge(manager)

    async def ping(args: list[str]) -> str:
        return "pong"

    bridge.expose("ping", ping, description="Health check")

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await tg.inject("/ping", command="ping")
    await dc.inject("/ping", command="ping")
    await asyncio.sleep(0.3)

    assert len(tg.sent) == 1
    assert tg.sent[0].text == "pong"
    assert len(dc.sent) == 1
    assert dc.sent[0].text == "pong"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_help_command_shows_all_exposed():
    """Bridge /help includes all exposed commands."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)
    bridge = ServiceBridge(manager)

    bridge.expose("deploy", lambda a: "ok", description="Deploy application")
    bridge.expose("restart", lambda a: "ok", description="Restart service")
    bridge.expose_status(lambda a: "ok")

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    await adapter.inject("/help", command="help")
    await asyncio.sleep(0.2)

    assert len(adapter.sent) == 1
    help_text = adapter.sent[0].text
    assert "/deploy" in help_text
    assert "Deploy application" in help_text
    assert "/restart" in help_text
    assert "/status" in help_text
    assert "/help" in help_text

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_error_recovery_between_messages():
    """Pipeline recovers after one message causes an error."""
    adapter = MockAdapter()
    manager = ChannelManager()
    manager.add_channel(adapter)

    cmds = CommandMiddleware()
    manager.add_middleware(cmds)

    @cmds.command("crash")
    async def crash(msg):
        raise RuntimeError("intentional crash")

    @cmds.command("ok")
    async def ok(msg):
        return "all good"

    task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.05)

    # First: crash
    await adapter.inject("/crash", command="crash")
    await asyncio.sleep(0.2)

    # Second: should still work
    await adapter.inject("/ok", command="ok")
    await asyncio.sleep(0.2)

    # Both messages processed (crash is logged but doesn't break pipeline)
    assert len(adapter.sent) >= 1
    # The ok command should produce a reply
    ok_replies = [m for m in adapter.sent if m.text == "all good"]
    assert len(ok_replies) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
