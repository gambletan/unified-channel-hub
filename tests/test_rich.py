"""Tests for RichReply — fluent API, rendering, and channel output."""

from __future__ import annotations

import pytest

from unified_channel.rich import RichReply, SectionType
from unified_channel.types import Button


def test_fluent_api_chaining():
    """All add_* methods return self for chaining."""
    reply = (
        RichReply("intro")
        .add_text("more text")
        .add_table(["A", "B"], [["1", "2"]])
        .add_buttons([[Button(label="OK", callback_data="ok")]])
        .add_image("https://img.example.com/photo.png", alt="photo")
        .add_code("print('hi')", language="python")
        .add_divider()
    )
    assert isinstance(reply, RichReply)
    # intro + 6 add_* calls = 7 sections
    assert len(reply.sections) == 7


def test_plain_text_basic():
    reply = RichReply("Hello world")
    assert reply.to_plain_text() == "Hello world"


def test_plain_text_table():
    reply = RichReply().add_table(
        headers=["Name", "Score"],
        rows=[["Alice", "95"], ["Bob", "87"]],
    )
    text = reply.to_plain_text()
    assert "Name" in text
    assert "Alice" in text
    assert "---" in text or "-+-" in text


def test_plain_text_buttons():
    reply = RichReply().add_buttons([
        [Button(label="Yes", callback_data="y"), Button(label="No", url="https://example.com")],
    ])
    text = reply.to_plain_text()
    assert "[Yes]" in text
    assert "[No](https://example.com)" in text


def test_plain_text_code_block():
    reply = RichReply().add_code("x = 1", language="python")
    text = reply.to_plain_text()
    assert "```python" in text
    assert "x = 1" in text


def test_to_telegram_with_buttons():
    reply = (
        RichReply("Hello")
        .add_buttons([[
            Button(label="Go", url="https://example.com"),
            Button(label="Click", callback_data="cb1"),
        ]])
    )
    tg = reply.to_telegram()
    assert tg["parse_mode"] == "Markdown"
    assert "Hello" in tg["text"]
    kb = tg["reply_markup"]["inline_keyboard"]
    assert len(kb) == 1
    assert kb[0][0]["url"] == "https://example.com"
    assert kb[0][1]["callback_data"] == "cb1"


def test_to_discord_embed():
    reply = RichReply("Some text").add_divider().add_text("More")
    dc = reply.to_discord()
    assert len(dc["embeds"]) == 1
    assert "Some text" in dc["embeds"][0]["description"]


def test_to_slack_blocks():
    reply = (
        RichReply("Header")
        .add_divider()
        .add_buttons([[Button(label="Act", callback_data="act")]])
    )
    sl = reply.to_slack()
    block_types = [b["type"] for b in sl["blocks"]]
    assert "section" in block_types
    assert "divider" in block_types
    assert "actions" in block_types


def test_to_outbound_telegram():
    reply = RichReply("hi")
    out = reply.to_outbound("telegram")
    assert out.text == "hi"
    assert out.parse_mode == "Markdown"


def test_to_outbound_unknown_channel_plain_text():
    reply = RichReply("plain")
    out = reply.to_outbound("sms")
    assert out.text == "plain"
    assert out.metadata.get("_rich") is None


def test_to_outbound_discord():
    reply = RichReply("dc msg")
    out = reply.to_outbound("discord")
    assert "_rich" in out.metadata
    assert "embeds" in out.metadata["_rich"]


def test_empty_reply():
    reply = RichReply()
    assert reply.to_plain_text() == ""
    tg = reply.to_telegram()
    assert tg["text"] == ""
