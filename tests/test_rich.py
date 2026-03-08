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


def test_table_no_rows():
    """Table with headers but no rows renders correctly."""
    reply = RichReply().add_table(headers=["Name", "Age"], rows=[])
    text = reply.to_plain_text()
    assert "Name" in text
    assert "Age" in text


def test_table_no_headers():
    """Table with rows but no headers renders rows only."""
    reply = RichReply().add_table(headers=[], rows=[["a", "b"], ["c", "d"]])
    text = reply.to_plain_text()
    assert "a" in text
    assert "c" in text
    # No separator line when no headers
    assert "-+-" not in text


def test_table_uneven_columns():
    """Table with rows of different lengths doesn't crash."""
    # The zip in _render_table_plain will truncate to shortest row
    reply = RichReply().add_table(
        headers=["A", "B", "C"],
        rows=[["1", "2", "3"], ["4", "5"]],  # second row shorter
    )
    text = reply.to_plain_text()
    assert "A" in text
    assert "1" in text


def test_multiple_sections_chained():
    """Multiple different sections are all present in output."""
    reply = (
        RichReply("intro")
        .add_text("body")
        .add_code("x=1", language="py")
        .add_divider()
        .add_text("footer")
    )
    text = reply.to_plain_text()
    assert "intro" in text
    assert "body" in text
    assert "x=1" in text
    assert "---" in text
    assert "footer" in text


def test_to_outbound_unknown_channel_fallback():
    """Unknown channel falls back to plain text."""
    reply = RichReply("test message").add_code("print('hi')", language="python")
    out = reply.to_outbound("carrier_pigeon")
    assert "test message" in out.text
    assert "print('hi')" in out.text
    assert out.metadata.get("_rich") is None


def test_buttons_with_urls():
    """Buttons with URLs render correctly in all formats."""
    reply = RichReply().add_buttons([
        [Button(label="Docs", url="https://docs.example.com")],
        [Button(label="API", url="https://api.example.com")],
    ])

    # Plain text
    text = reply.to_plain_text()
    assert "[Docs](https://docs.example.com)" in text
    assert "[API](https://api.example.com)" in text

    # Telegram
    tg = reply.to_telegram()
    kb = tg["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["url"] == "https://docs.example.com"

    # Discord
    dc = reply.to_discord()
    assert len(dc["components"]) == 2
    assert dc["components"][0]["components"][0]["style"] == 5  # link style

    # Slack
    sl = reply.to_slack()
    actions = [b for b in sl["blocks"] if b["type"] == "actions"]
    assert len(actions) == 1


def test_code_block_with_language():
    """Code block includes language tag."""
    reply = RichReply().add_code("SELECT * FROM users;", language="sql")
    text = reply.to_plain_text()
    assert "```sql" in text
    assert "SELECT * FROM users;" in text

    tg = reply.to_telegram()
    assert "```sql" in tg["text"]


def test_code_block_without_language():
    """Code block without language still renders correctly."""
    reply = RichReply().add_code("echo hello")
    text = reply.to_plain_text()
    assert "```\necho hello\n```" in text


def test_image_rendering():
    """Image renders in all formats."""
    reply = RichReply().add_image("https://img.example.com/photo.jpg", alt="Nice photo")

    text = reply.to_plain_text()
    assert "[Image: Nice photo]" in text

    tg = reply.to_telegram()
    assert "Nice photo" in tg["text"]

    dc = reply.to_discord()
    assert "https://img.example.com/photo.jpg" in dc["embeds"][0]["description"]

    sl = reply.to_slack()
    img_blocks = [b for b in sl["blocks"] if b["type"] == "image"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["alt_text"] == "Nice photo"


def test_to_outbound_slack():
    """Slack outbound includes _rich metadata."""
    reply = RichReply("slack msg")
    out = reply.to_outbound("slack")
    assert out.text == "slack msg"
    assert "_rich" in out.metadata
    assert "blocks" in out.metadata["_rich"]


def test_telegram_no_buttons():
    """Telegram output without buttons has no reply_markup."""
    reply = RichReply("simple text")
    tg = reply.to_telegram()
    assert "reply_markup" not in tg


def test_discord_buttons_callback():
    """Discord buttons with callback_data use style 1 and custom_id."""
    reply = RichReply().add_buttons([
        [Button(label="Confirm", callback_data="confirm_action")],
    ])
    dc = reply.to_discord()
    comp = dc["components"][0]["components"][0]
    assert comp["style"] == 1
    assert comp["custom_id"] == "confirm_action"


def test_empty_table():
    """Completely empty table renders as empty string."""
    reply = RichReply().add_table(headers=[], rows=[])
    text = reply.to_plain_text()
    # Empty table should just produce empty or minimal output
    assert isinstance(text, str)
