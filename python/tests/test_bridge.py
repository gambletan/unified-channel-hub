"""Tests for ServiceBridge."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from unified_channel.bridge import ServiceBridge
from unified_channel.manager import ChannelManager
from unified_channel.adapter import ChannelAdapter
from unified_channel.types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(
    command: str | None = None,
    args: list[str] | None = None,
    text: str = "",
) -> UnifiedMessage:
    ctype = ContentType.COMMAND if command else ContentType.TEXT
    return UnifiedMessage(
        id="m1",
        channel="test",
        sender=Identity(id="u1", username="tester"),
        content=MessageContent(type=ctype, text=text, command=command, args=args or []),
        chat_id="c1",
    )


class _StubAdapter(ChannelAdapter):
    """Minimal adapter that yields nothing (we drive the pipeline manually)."""

    channel_id = "test"

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        return
        yield  # type: ignore[misc]

    async def send(self, msg: OutboundMessage) -> str | None:
        return None

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(connected=True, channel="test")


def _make_bridge() -> tuple[ServiceBridge, ChannelManager]:
    manager = ChannelManager()
    manager.add_channel(_StubAdapter())
    bridge = ServiceBridge(manager)
    return bridge, manager


async def _run_pipeline(manager: ChannelManager, msg: UnifiedMessage) -> str | None:
    """Drive the manager pipeline directly for testing."""
    result = await manager._run_pipeline(msg)
    if isinstance(result, str):
        return result
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expose_basic_command():
    """expose() registers a command that returns a string."""
    bridge, manager = _make_bridge()

    async def greet(args: list[str]) -> str:
        return "hello"

    bridge.expose("greet", greet, description="Say hello")

    result = await _run_pipeline(manager, _make_msg(command="greet"))
    assert result == "hello"


@pytest.mark.asyncio
async def test_expose_with_positional_args():
    """Handler receives positional args from message."""
    bridge, manager = _make_bridge()

    async def echo(args: list[str]) -> str:
        return " ".join(args)

    bridge.expose("echo", echo)

    result = await _run_pipeline(manager, _make_msg(command="echo", args=["foo", "bar"]))
    assert result == "foo bar"


@pytest.mark.asyncio
async def test_expose_sync_handler():
    """Sync handlers are auto-wrapped to async."""
    bridge, manager = _make_bridge()

    def sync_ping(args: list[str]) -> str:
        return "pong"

    bridge.expose("ping", sync_ping)

    result = await _run_pipeline(manager, _make_msg(command="ping"))
    assert result == "pong"


@pytest.mark.asyncio
async def test_expose_handler_with_msg():
    """Handler that accepts (args, msg) receives the UnifiedMessage."""
    bridge, manager = _make_bridge()

    async def who(args: list[str], msg: UnifiedMessage) -> str:
        return f"you are {msg.sender.username} on {msg.channel}"

    bridge.expose("who", who)

    result = await _run_pipeline(manager, _make_msg(command="who"))
    assert result == "you are tester on test"


@pytest.mark.asyncio
async def test_expose_status():
    """expose_status() maps to /status."""
    bridge, manager = _make_bridge()

    async def my_status(args: list[str]) -> str:
        return "all systems go"

    bridge.expose_status(my_status)

    result = await _run_pipeline(manager, _make_msg(command="status"))
    assert result == "all systems go"


@pytest.mark.asyncio
async def test_expose_logs():
    """expose_logs() maps to /logs."""
    bridge, manager = _make_bridge()

    async def my_logs(args: list[str]) -> str:
        return "line1\nline2"

    bridge.expose_logs(my_logs)

    result = await _run_pipeline(manager, _make_msg(command="logs"))
    assert result == "line1\nline2"


@pytest.mark.asyncio
async def test_auto_help_generation():
    """/help lists all registered commands with descriptions."""
    bridge, manager = _make_bridge()

    bridge.expose("deploy", lambda a: "ok", description="Deploy the app", params=["env"])
    bridge.expose("restart", lambda a: "ok", description="Restart services")

    result = await _run_pipeline(manager, _make_msg(command="help"))
    assert result is not None
    assert "/help" in result
    assert "/deploy <env>" in result
    assert "Deploy the app" in result
    assert "/restart" in result
    assert "Restart services" in result


@pytest.mark.asyncio
async def test_error_handling():
    """Exceptions in command handlers return a friendly error message."""
    bridge, manager = _make_bridge()

    async def boom(args: list[str]) -> str:
        raise RuntimeError("kaboom")

    bridge.expose("boom", boom)

    result = await _run_pipeline(manager, _make_msg(command="boom"))
    assert result is not None
    assert "Error running /boom" in result
    assert "kaboom" in result


@pytest.mark.asyncio
async def test_flag_parsing():
    """--flag values are parsed and available via msg.metadata['_flags']."""
    bridge, manager = _make_bridge()
    captured_flags: dict[str, str] = {}

    async def deploy(args: list[str], msg: UnifiedMessage) -> str:
        captured_flags.update(msg.metadata.get("_flags", {}))
        return f"env={args[0] if args else 'none'}"

    bridge.expose("deploy", deploy)

    msg = _make_msg(command="deploy", args=["staging", "--force", "--count", "3"])
    result = await _run_pipeline(manager, msg)
    assert result == "env=staging"
    assert captured_flags == {"force": "true", "count": "3"}


@pytest.mark.asyncio
async def test_flag_equals_syntax():
    """--key=value flag syntax is parsed correctly."""
    bridge, manager = _make_bridge()
    captured_flags: dict[str, str] = {}

    async def cmd(args: list[str], msg: UnifiedMessage) -> str:
        captured_flags.update(msg.metadata.get("_flags", {}))
        return "ok"

    bridge.expose("cmd", cmd)

    msg = _make_msg(command="cmd", args=["--env=prod", "--verbose"])
    await _run_pipeline(manager, msg)
    assert captured_flags == {"env": "prod", "verbose": "true"}


@pytest.mark.asyncio
async def test_help_is_builtin():
    """/help is available even with no user-exposed commands."""
    bridge, manager = _make_bridge()

    result = await _run_pipeline(manager, _make_msg(command="help"))
    assert result is not None
    assert "Available commands" in result
    assert "/help" in result


@pytest.mark.asyncio
async def test_sync_handler_with_msg():
    """Sync handler that accepts (args, msg) is correctly detected and wrapped."""
    bridge, manager = _make_bridge()

    def sync_who(args: list[str], msg: UnifiedMessage) -> str:
        return f"sender={msg.sender.id}"

    bridge.expose("who", sync_who)

    result = await _run_pipeline(manager, _make_msg(command="who"))
    assert result == "sender=u1"


@pytest.mark.asyncio
async def test_expose_handler_raises_exception():
    """Exception in handler returns friendly error message."""
    bridge, manager = _make_bridge()

    async def fail(args: list[str]) -> str:
        raise ValueError("something broke")

    bridge.expose("fail", fail)

    result = await _run_pipeline(manager, _make_msg(command="fail"))
    assert result is not None
    assert "Error running /fail" in result
    assert "something broke" in result


@pytest.mark.asyncio
async def test_expose_no_arg_handler():
    """Handler with no parameters still works (receives args but ignores)."""
    bridge, manager = _make_bridge()

    async def noargs(args: list[str]) -> str:
        return "no args needed"

    bridge.expose("noargs", noargs)

    result = await _run_pipeline(manager, _make_msg(command="noargs"))
    assert result == "no args needed"


@pytest.mark.asyncio
async def test_expose_status_async():
    """expose_status with async handler works."""
    bridge, manager = _make_bridge()

    async def status_check(args: list[str]) -> str:
        return "healthy: 42 connections"

    bridge.expose_status(status_check)

    result = await _run_pipeline(manager, _make_msg(command="status"))
    assert result == "healthy: 42 connections"


@pytest.mark.asyncio
async def test_multiple_commands_in_sequence():
    """Multiple commands can be called in sequence."""
    bridge, manager = _make_bridge()

    async def cmd_a(args: list[str]) -> str:
        return "result_a"

    async def cmd_b(args: list[str]) -> str:
        return "result_b"

    bridge.expose("a", cmd_a)
    bridge.expose("b", cmd_b)

    result_a = await _run_pipeline(manager, _make_msg(command="a"))
    result_b = await _run_pipeline(manager, _make_msg(command="b"))
    result_a2 = await _run_pipeline(manager, _make_msg(command="a"))

    assert result_a == "result_a"
    assert result_b == "result_b"
    assert result_a2 == "result_a"


@pytest.mark.asyncio
async def test_flag_parsing_mixed():
    """--flag parsing with mixed positional and flag args."""
    bridge, manager = _make_bridge()
    captured = {}

    async def deploy(args: list[str], msg: UnifiedMessage) -> str:
        captured["args"] = args
        captured["flags"] = msg.metadata.get("_flags", {})
        return "ok"

    bridge.expose("deploy", deploy)

    msg = _make_msg(command="deploy", args=["prod", "--force", "--count", "5", "extra"])
    await _run_pipeline(manager, msg)
    assert captured["args"] == ["prod", "extra"]
    assert captured["flags"]["force"] == "true"
    assert captured["flags"]["count"] == "5"


@pytest.mark.asyncio
async def test_unknown_command_returns_none():
    """Unknown command falls through (no help suggestion, just None)."""
    bridge, manager = _make_bridge()

    result = await _run_pipeline(manager, _make_msg(command="nonexistent"))
    # Falls through to fallback handler (None by default)
    assert result is None


@pytest.mark.asyncio
async def test_empty_args_handling():
    """Handler called with empty args list."""
    bridge, manager = _make_bridge()

    async def cmd(args: list[str]) -> str:
        return f"count={len(args)}"

    bridge.expose("cmd", cmd)

    result = await _run_pipeline(manager, _make_msg(command="cmd", args=[]))
    assert result == "count=0"


@pytest.mark.asyncio
async def test_handler_returns_none():
    """Handler returning None produces None result."""
    bridge, manager = _make_bridge()

    async def silent(args: list[str]) -> str:
        return None  # type: ignore[return-value]

    bridge.expose("silent", silent)

    result = await _run_pipeline(manager, _make_msg(command="silent"))
    assert result is None


@pytest.mark.asyncio
async def test_large_argument_list():
    """Handler receives many arguments correctly."""
    bridge, manager = _make_bridge()

    async def many_args(args: list[str]) -> str:
        return f"count={len(args)}"

    bridge.expose("many", many_args)

    args = [f"arg{i}" for i in range(50)]
    result = await _run_pipeline(manager, _make_msg(command="many", args=args))
    assert result == "count=50"


@pytest.mark.asyncio
async def test_custom_prefix():
    """ServiceBridge with custom prefix still generates help with that prefix."""
    manager = ChannelManager()
    manager.add_channel(_StubAdapter())
    bridge = ServiceBridge(manager, prefix="!")

    async def ping(args: list[str]) -> str:
        return "pong"

    bridge.expose("ping", ping, description="Ping test")

    result = await _run_pipeline(manager, _make_msg(command="help"))
    assert result is not None
    assert "!help" in result
    assert "!ping" in result
