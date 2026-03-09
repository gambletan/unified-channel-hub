"""Tests for core types."""

from __future__ import annotations

from datetime import datetime

from unified_channel.types import (
    Button,
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)


def test_content_type_values():
    assert ContentType.TEXT.value == "text"
    assert ContentType.COMMAND.value == "command"
    assert ContentType.MEDIA.value == "media"
    assert ContentType.REACTION.value == "reaction"
    assert ContentType.EDIT.value == "edit"
    assert ContentType.CALLBACK.value == "callback"


def test_identity_defaults():
    ident = Identity(id="123")
    assert ident.id == "123"
    assert ident.username is None
    assert ident.display_name is None


def test_identity_full():
    ident = Identity(id="123", username="bob", display_name="Bob Smith")
    assert ident.username == "bob"
    assert ident.display_name == "Bob Smith"


def test_message_content_text():
    mc = MessageContent(type=ContentType.TEXT, text="hello")
    assert mc.command is None
    assert mc.args == []
    assert mc.media_url is None


def test_message_content_command():
    mc = MessageContent(
        type=ContentType.COMMAND, text="/run job1", command="run", args=["job1"]
    )
    assert mc.command == "run"
    assert mc.args == ["job1"]


def test_message_content_media():
    mc = MessageContent(
        type=ContentType.MEDIA, text="", media_url="https://example.com/img.jpg",
        media_type="image",
    )
    assert mc.media_url == "https://example.com/img.jpg"
    assert mc.media_type == "image"


def test_message_content_callback():
    mc = MessageContent(
        type=ContentType.CALLBACK, text="confirm_yes", callback_data="confirm_yes"
    )
    assert mc.callback_data == "confirm_yes"


def test_unified_message_defaults():
    msg = UnifiedMessage(
        id="1", channel="test",
        sender=Identity(id="u1"),
        content=MessageContent(type=ContentType.TEXT, text="hi"),
    )
    assert msg.thread_id is None
    assert msg.reply_to_id is None
    assert msg.chat_id is None
    assert msg.raw is None
    assert msg.metadata == {}
    assert isinstance(msg.timestamp, datetime)


def test_unified_message_full():
    msg = UnifiedMessage(
        id="1", channel="telegram",
        sender=Identity(id="u1", username="bob"),
        content=MessageContent(type=ContentType.TEXT, text="hello"),
        chat_id="chat1", thread_id="thread1", reply_to_id="msg0",
        metadata={"key": "val"},
    )
    assert msg.chat_id == "chat1"
    assert msg.thread_id == "thread1"
    assert msg.reply_to_id == "msg0"
    assert msg.metadata["key"] == "val"


def test_outbound_message_defaults():
    out = OutboundMessage(chat_id="chat1", text="hello")
    assert out.reply_to_id is None
    assert out.media_url is None
    assert out.parse_mode is None
    assert out.buttons is None
    assert out.metadata == {}


def test_outbound_message_with_buttons():
    out = OutboundMessage(
        chat_id="chat1", text="Choose",
        buttons=[[Button(label="Yes", callback_data="y"), Button(label="No", callback_data="n")]],
    )
    assert len(out.buttons) == 1
    assert len(out.buttons[0]) == 2
    assert out.buttons[0][0].label == "Yes"
    assert out.buttons[0][1].callback_data == "n"


def test_button_url():
    b = Button(label="Visit", url="https://example.com")
    assert b.url == "https://example.com"
    assert b.callback_data is None


def test_channel_status():
    status = ChannelStatus(connected=True, channel="telegram", account_id="@bot")
    assert status.connected is True
    assert status.error is None
    assert status.last_activity is None
