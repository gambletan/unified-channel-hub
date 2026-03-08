"""Unit tests for individual adapters — mocked external dependencies."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from types import ModuleType
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unified_channel.types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


# ── iMessage adapter tests (no external deps, macOS only) ─────────────────

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
class TestIMessageAdapter:
    def test_channel_id(self):
        from unified_channel.adapters.imessage import IMessageAdapter
        adapter = IMessageAdapter()
        assert adapter.channel_id == "imessage"

    @pytest.mark.asyncio
    async def test_get_status_disconnected(self):
        from unified_channel.adapters.imessage import IMessageAdapter
        adapter = IMessageAdapter()
        status = await adapter.get_status()
        assert status.connected is False
        assert status.channel == "imessage"


# ── IRC adapter tests (no external deps) ──────────────────────────────────

class TestIRCAdapter:
    def test_channel_id(self):
        from unified_channel.adapters.irc import IRCAdapter
        adapter = IRCAdapter(server="irc.test.com", nickname="testbot")
        assert adapter.channel_id == "irc"

    @pytest.mark.asyncio
    async def test_get_status_disconnected(self):
        from unified_channel.adapters.irc import IRCAdapter
        adapter = IRCAdapter(server="irc.test.com")
        status = await adapter.get_status()
        assert status.connected is False
        assert status.channel == "irc"
        assert "irc.test.com" in status.account_id

    @pytest.mark.asyncio
    async def test_process_privmsg(self):
        from unified_channel.adapters.irc import IRCAdapter
        adapter = IRCAdapter(server="irc.test.com", command_prefix="!")
        adapter._connected = True

        await adapter._process_line(":bob!bob@host PRIVMSG #test :hello world")
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.TEXT
        assert msg.content.text == "hello world"
        assert msg.sender.id == "bob"
        assert msg.chat_id == "#test"

    @pytest.mark.asyncio
    async def test_process_command(self):
        from unified_channel.adapters.irc import IRCAdapter
        adapter = IRCAdapter(server="irc.test.com", command_prefix="!")
        adapter._connected = True

        await adapter._process_line(":alice!a@h PRIVMSG #chan :!status arg1")
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.COMMAND
        assert msg.content.command == "status"
        assert msg.content.args == ["arg1"]

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self):
        from unified_channel.adapters.irc import IRCAdapter
        adapter = IRCAdapter(server="irc.test.com", nickname="mybot")
        adapter._connected = True

        await adapter._process_line(":mybot!m@h PRIVMSG #test :self message")
        assert adapter._queue.empty()

    @pytest.mark.asyncio
    async def test_dm_chat_id(self):
        """DM target is bot's nick — chat_id should be sender."""
        from unified_channel.adapters.irc import IRCAdapter
        adapter = IRCAdapter(server="irc.test.com", nickname="mybot")
        adapter._connected = True

        await adapter._process_line(":alice!a@h PRIVMSG mybot :hello dm")
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.chat_id == "alice"  # reply to sender, not to ourselves


# ── Adapter lazy import tests ─────────────────────────────────────────────

class TestLazyImports:
    """Verify that adapters are importable via lazy __getattr__."""

    def test_irc_import(self):
        from unified_channel import IRCAdapter
        assert IRCAdapter.channel_id == "irc"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_imessage_import(self):
        from unified_channel import IMessageAdapter
        assert IMessageAdapter.channel_id == "imessage"

    def test_unknown_import_raises(self):
        with pytest.raises(ImportError):
            from unified_channel import NonExistentAdapter  # noqa: F401

    def test_all_adapter_names_in_all(self):
        import unified_channel
        expected = [
            "TelegramAdapter", "DiscordAdapter", "SlackAdapter",
            "LineAdapter", "MatrixAdapter", "MSTeamsAdapter",
            "FeishuAdapter", "WhatsAppAdapter", "IMessageAdapter",
            "MattermostAdapter", "GoogleChatAdapter", "NextcloudTalkAdapter",
            "SynologyChatAdapter", "ZaloAdapter", "NostrAdapter",
            "BlueBubblesAdapter", "TwitchAdapter", "IRCAdapter",
        ]
        for name in expected:
            assert name in unified_channel.__all__


# ── WhatsApp message parsing (no SDK needed) ──────────────────────────────

class TestWhatsAppParsing:
    """Test WhatsApp webhook message parsing without running server."""

    @pytest.fixture
    def adapter(self):
        # Mock httpx so import works
        mock_httpx = MagicMock()
        with patch.dict(sys.modules, {"httpx": mock_httpx, "aiohttp": MagicMock(), "aiohttp.web": MagicMock()}):
            from unified_channel.adapters.whatsapp import WhatsAppAdapter
            return WhatsAppAdapter(
                access_token="test",
                phone_number_id="123",
                verify_token="verify",
            )

    @pytest.mark.asyncio
    async def test_process_text_message(self, adapter):
        wa_msg = {
            "id": "wamid.123",
            "from": "+1234567890",
            "type": "text",
            "timestamp": "1700000000",
            "text": {"body": "hello"},
        }
        await adapter._process_message(wa_msg, {"+1234567890": "John"})
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.TEXT
        assert msg.content.text == "hello"
        assert msg.sender.id == "+1234567890"
        assert msg.sender.display_name == "John"

    @pytest.mark.asyncio
    async def test_process_command(self, adapter):
        wa_msg = {
            "id": "wamid.456",
            "from": "+1234567890",
            "type": "text",
            "timestamp": "1700000000",
            "text": {"body": "/status"},
        }
        await adapter._process_message(wa_msg, {})
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.COMMAND
        assert msg.content.command == "status"

    @pytest.mark.asyncio
    async def test_process_image(self, adapter):
        wa_msg = {
            "id": "wamid.789",
            "from": "+1234567890",
            "type": "image",
            "timestamp": "1700000000",
            "image": {"id": "media123", "caption": "look at this"},
        }
        await adapter._process_message(wa_msg, {})
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.MEDIA
        assert msg.content.media_type == "image"
        assert msg.content.text == "look at this"

    @pytest.mark.asyncio
    async def test_process_reaction(self, adapter):
        wa_msg = {
            "id": "wamid.r1",
            "from": "+1234567890",
            "type": "reaction",
            "timestamp": "1700000000",
            "reaction": {"emoji": "👍"},
        }
        await adapter._process_message(wa_msg, {})
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.REACTION
        assert msg.content.text == "👍"

    @pytest.mark.asyncio
    async def test_process_reply_context(self, adapter):
        wa_msg = {
            "id": "wamid.reply1",
            "from": "+1234567890",
            "type": "text",
            "timestamp": "1700000000",
            "text": {"body": "reply text"},
            "context": {"id": "wamid.original"},
        }
        await adapter._process_message(wa_msg, {})
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.reply_to_id == "wamid.original"


# ── Mattermost post parsing ──────────────────────────────────────────────

class TestMattermostParsing:
    @pytest.fixture
    def adapter(self):
        with patch.dict(sys.modules, {"httpx": MagicMock(), "websockets": MagicMock()}):
            from unified_channel.adapters.mattermost import MattermostAdapter
            a = MattermostAdapter(url="https://mm.test", token="tok")
            a._bot_user_id = "bot123"
            a._connected = True
            return a

    @pytest.mark.asyncio
    async def test_process_text(self, adapter):
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps({
                    "id": "post1", "user_id": "user1",
                    "channel_id": "ch1", "message": "hello",
                }),
            },
        }
        await adapter._process_post(event)
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.text == "hello"
        assert msg.sender.id == "user1"

    @pytest.mark.asyncio
    async def test_process_command(self, adapter):
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps({
                    "id": "post2", "user_id": "user1",
                    "channel_id": "ch1", "message": "/deploy staging",
                }),
            },
        }
        await adapter._process_post(event)
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.COMMAND
        assert msg.content.command == "deploy"
        assert msg.content.args == ["staging"]

    @pytest.mark.asyncio
    async def test_ignores_own_posts(self, adapter):
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps({
                    "id": "post3", "user_id": "bot123",
                    "channel_id": "ch1", "message": "echo",
                }),
            },
        }
        await adapter._process_post(event)
        assert adapter._queue.empty()

    @pytest.mark.asyncio
    async def test_thread_id(self, adapter):
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps({
                    "id": "post4", "user_id": "user1",
                    "channel_id": "ch1", "message": "threaded",
                    "root_id": "root1",
                }),
            },
        }
        await adapter._process_post(event)
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.thread_id == "root1"


# ── Zalo message parsing ─────────────────────────────────────────────────

class TestZaloParsing:
    @pytest.fixture
    def adapter(self):
        with patch.dict(sys.modules, {"httpx": MagicMock(), "aiohttp": MagicMock(), "aiohttp.web": MagicMock()}):
            from unified_channel.adapters.zalo import ZaloAdapter
            return ZaloAdapter(access_token="tok")

    @pytest.mark.asyncio
    async def test_process_text(self, adapter):
        body = {
            "sender": {"id": "zalo_user1"},
            "message": {"msg_id": "m1", "text": "xin chao"},
        }
        await adapter._process_text(body)
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.text == "xin chao"
        assert msg.sender.id == "zalo_user1"

    @pytest.mark.asyncio
    async def test_process_command(self, adapter):
        body = {
            "sender": {"id": "u1"},
            "message": {"msg_id": "m2", "text": "/status"},
        }
        await adapter._process_text(body)
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.COMMAND
        assert msg.content.command == "status"


# ── BlueBubbles message parsing ──────────────────────────────────────────

class TestBlueBubblesParsing:
    @pytest.fixture
    def adapter(self):
        with patch.dict(sys.modules, {"httpx": MagicMock()}):
            from unified_channel.adapters.bluebubbles import BlueBubblesAdapter
            a = BlueBubblesAdapter(server_url="http://localhost:1234", password="pw")
            a._connected = True
            a._http = MagicMock()
            return a

    def test_channel_id(self, adapter):
        assert adapter.channel_id == "bluebubbles"

    @pytest.mark.asyncio
    async def test_status(self, adapter):
        status = await adapter.get_status()
        assert status.connected is True
        assert status.channel == "bluebubbles"


# ── Twitch message parsing ───────────────────────────────────────────────

class TestTwitchParsing:
    @pytest.fixture
    def adapter(self):
        with patch.dict(sys.modules, {"websockets": MagicMock()}):
            from unified_channel.adapters.twitch import TwitchAdapter
            a = TwitchAdapter(
                oauth_token="oauth:test", bot_username="testbot",
                channels=["#testchan"], command_prefix="!",
            )
            a._connected = True
            return a

    @pytest.mark.asyncio
    async def test_process_text(self, adapter):
        line = ":alice!alice@alice.tmi.twitch.tv PRIVMSG #testchan :hello chat"
        await adapter._process_line(line)
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.text == "hello chat"
        assert msg.sender.username == "alice"
        assert msg.chat_id == "testchan"

    @pytest.mark.asyncio
    async def test_process_command(self, adapter):
        line = ":bob!bob@bob.tmi.twitch.tv PRIVMSG #testchan :!roll 6"
        await adapter._process_line(line)
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.content.type == ContentType.COMMAND
        assert msg.content.command == "roll"
        assert msg.content.args == ["6"]

    @pytest.mark.asyncio
    async def test_ignores_self(self, adapter):
        line = ":testbot!testbot@testbot.tmi.twitch.tv PRIVMSG #testchan :echo"
        await adapter._process_line(line)
        assert adapter._queue.empty()

    @pytest.mark.asyncio
    async def test_tags_parsing(self, adapter):
        line = "@user-id=12345;display-name=Alice :alice!alice@alice.tmi.twitch.tv PRIVMSG #testchan :hi"
        await adapter._process_line(line)
        msg = await asyncio.wait_for(adapter._queue.get(), timeout=1)
        assert msg.sender.id == "12345"
        assert msg.sender.display_name == "Alice"


# ── Synology Chat parsing ────────────────────────────────────────────────

class TestSynologyChatParsing:
    @pytest.fixture
    def adapter(self):
        with patch.dict(sys.modules, {"httpx": MagicMock(), "aiohttp": MagicMock(), "aiohttp.web": MagicMock()}):
            from unified_channel.adapters.synology_chat import SynologyChatAdapter
            return SynologyChatAdapter(incoming_webhook_url="https://nas/hook")

    def test_channel_id(self, adapter):
        assert adapter.channel_id == "synology-chat"

    @pytest.mark.asyncio
    async def test_status(self, adapter):
        status = await adapter.get_status()
        assert status.connected is False


# ── Nextcloud Talk ────────────────────────────────────────────────────────

class TestNextcloudTalkAdapter:
    @pytest.fixture
    def adapter(self):
        with patch.dict(sys.modules, {"httpx": MagicMock()}):
            from unified_channel.adapters.nextcloud_talk import NextcloudTalkAdapter
            return NextcloudTalkAdapter(
                server_url="https://nc.test",
                username="bot", password="pass",
            )

    def test_channel_id(self, adapter):
        assert adapter.channel_id == "nextcloud-talk"

    @pytest.mark.asyncio
    async def test_status(self, adapter):
        status = await adapter.get_status()
        assert status.connected is False
        assert status.account_id == "bot"
