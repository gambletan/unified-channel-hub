"""Tests for rich media normalization — unified attachment model."""

from __future__ import annotations

import pytest

from unified_channel.media import (
    Attachment,
    MediaNormalizerMiddleware,
    MediaType,
    detect_media_type,
    normalize_attachment,
)
from unified_channel.types import (
    ContentType,
    Identity,
    MessageContent,
    UnifiedMessage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(
    *,
    content_type: ContentType = ContentType.TEXT,
    text: str = "",
    media_url: str | None = None,
    media_type: str | None = None,
    raw=None,
    channel: str = "telegram",
) -> UnifiedMessage:
    return UnifiedMessage(
        id="msg-1",
        channel=channel,
        sender=Identity(id="user-1", username="tester"),
        content=MessageContent(
            type=content_type,
            text=text,
            media_url=media_url,
            media_type=media_type,
        ),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Attachment dataclass
# ---------------------------------------------------------------------------

def test_attachment_creation_all_fields():
    att = Attachment(
        type=MediaType.IMAGE,
        url="https://example.com/photo.jpg",
        data=b"\xff\xd8",
        filename="photo.jpg",
        mime_type="image/jpeg",
        size=12345,
        width=1920,
        height=1080,
        duration=None,
        thumbnail_url="https://example.com/thumb.jpg",
        metadata={"file_id": "abc123"},
    )
    assert att.type == MediaType.IMAGE
    assert att.url == "https://example.com/photo.jpg"
    assert att.data == b"\xff\xd8"
    assert att.filename == "photo.jpg"
    assert att.mime_type == "image/jpeg"
    assert att.size == 12345
    assert att.width == 1920
    assert att.height == 1080
    assert att.duration is None
    assert att.thumbnail_url == "https://example.com/thumb.jpg"
    assert att.metadata == {"file_id": "abc123"}


def test_attachment_defaults():
    att = Attachment(type=MediaType.DOCUMENT)
    assert att.url is None
    assert att.data is None
    assert att.filename is None
    assert att.size is None
    assert att.metadata == {}


# ---------------------------------------------------------------------------
# MediaType enum
# ---------------------------------------------------------------------------

def test_media_type_values():
    assert MediaType.IMAGE == "image"
    assert MediaType.VIDEO == "video"
    assert MediaType.AUDIO == "audio"
    assert MediaType.VOICE == "voice"
    assert MediaType.DOCUMENT == "document"
    assert MediaType.STICKER == "sticker"
    assert MediaType.LOCATION == "location"
    assert MediaType.CONTACT == "contact"


def test_media_type_is_str():
    assert isinstance(MediaType.IMAGE, str)
    assert str(MediaType.IMAGE) == "MediaType.IMAGE" or MediaType.IMAGE.value == "image"
    assert MediaType.IMAGE.value == "image"


# ---------------------------------------------------------------------------
# detect_media_type
# ---------------------------------------------------------------------------

def test_detect_from_mime_image():
    assert detect_media_type(mime_type="image/jpeg") == MediaType.IMAGE


def test_detect_from_mime_video():
    assert detect_media_type(mime_type="video/mp4") == MediaType.VIDEO


def test_detect_from_mime_audio():
    assert detect_media_type(mime_type="audio/mpeg") == MediaType.AUDIO


def test_detect_from_filename_png():
    assert detect_media_type(filename="photo.png") == MediaType.IMAGE


def test_detect_from_filename_mp4():
    assert detect_media_type(filename="clip.mp4") == MediaType.VIDEO


def test_detect_from_url_extension():
    assert detect_media_type(url="https://cdn.example.com/files/song.mp3?token=abc") == MediaType.AUDIO


def test_detect_from_url_image():
    assert detect_media_type(url="https://images.example.com/pic.webp") == MediaType.IMAGE


def test_detect_fallback_to_document():
    assert detect_media_type() == MediaType.DOCUMENT
    assert detect_media_type(mime_type="application/octet-stream") == MediaType.DOCUMENT


def test_detect_mime_takes_priority_over_filename():
    # MIME says video, filename says .mp3 — MIME wins
    result = detect_media_type(mime_type="video/webm", filename="audio.mp3")
    assert result == MediaType.VIDEO


def test_detect_filename_takes_priority_over_url():
    result = detect_media_type(filename="doc.pdf", url="https://example.com/image.png")
    assert result == MediaType.DOCUMENT


# ---------------------------------------------------------------------------
# normalize_attachment — Telegram style
# ---------------------------------------------------------------------------

def test_normalize_telegram_photo():
    raw = {
        "file_id": "AgACAgIAAxk",
        "mime_type": "image/jpeg",
        "file_size": 54321,
        "width": 800,
        "height": 600,
        "file_url": "https://api.telegram.org/file/bot123/photos/photo.jpg",
    }
    att = normalize_attachment(raw, "telegram")
    assert att.type == MediaType.IMAGE
    assert att.url == "https://api.telegram.org/file/bot123/photos/photo.jpg"
    assert att.mime_type == "image/jpeg"
    assert att.size == 54321
    assert att.width == 800
    assert att.height == 600
    assert att.metadata["file_id"] == "AgACAgIAAxk"


def test_normalize_telegram_voice():
    raw = {
        "type": "voice",
        "file_id": "AwACAgIAAxk",
        "mime_type": "audio/ogg",
        "file_size": 9876,
        "duration": 5.2,
        "file_url": "https://api.telegram.org/file/bot123/voice/msg.ogg",
    }
    att = normalize_attachment(raw, "telegram")
    assert att.type == MediaType.VOICE
    assert att.duration == 5.2
    assert att.metadata["file_id"] == "AwACAgIAAxk"


# ---------------------------------------------------------------------------
# normalize_attachment — Discord style
# ---------------------------------------------------------------------------

def test_normalize_discord_attachment():
    raw = {
        "url": "https://cdn.discordapp.com/attachments/123/456/image.png",
        "proxy_url": "https://media.discordapp.net/attachments/123/456/image.png",
        "content_type": "image/png",
        "filename": "image.png",
        "size": 102400,
        "width": 1024,
        "height": 768,
    }
    att = normalize_attachment(raw, "discord")
    assert att.type == MediaType.IMAGE
    assert att.url == "https://cdn.discordapp.com/attachments/123/456/image.png"
    assert att.filename == "image.png"
    assert att.mime_type == "image/png"
    assert att.width == 1024


# ---------------------------------------------------------------------------
# normalize_attachment — location / contact
# ---------------------------------------------------------------------------

def test_normalize_location():
    raw = {
        "type": "location",
        "latitude": 37.7749,
        "longitude": -122.4194,
    }
    att = normalize_attachment(raw, "telegram")
    assert att.type == MediaType.LOCATION
    assert att.metadata["latitude"] == 37.7749
    assert att.metadata["longitude"] == -122.4194


def test_normalize_contact():
    raw = {
        "type": "contact",
        "phone_number": "+1234567890",
        "first_name": "Alice",
        "last_name": "Smith",
    }
    att = normalize_attachment(raw, "telegram")
    assert att.type == MediaType.CONTACT
    assert att.metadata["phone_number"] == "+1234567890"
    assert att.metadata["first_name"] == "Alice"


def test_normalize_sticker():
    raw = {
        "type": "sticker",
        "file_id": "sticker123",
        "mime_type": "image/webp",
        "width": 512,
        "height": 512,
        "file_url": "https://api.telegram.org/file/bot123/stickers/s.webp",
    }
    att = normalize_attachment(raw, "telegram")
    assert att.type == MediaType.STICKER
    assert att.width == 512


# ---------------------------------------------------------------------------
# MediaNormalizerMiddleware
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_middleware_adds_attachments_from_content():
    mw = MediaNormalizerMiddleware()
    msg = _make_msg(
        content_type=ContentType.MEDIA,
        media_url="https://example.com/photo.jpg",
        media_type="image/jpeg",
    )
    result = await mw.process(msg, _echo_handler)
    assert "attachments" in msg.metadata
    atts = msg.metadata["attachments"]
    assert len(atts) == 1
    assert atts[0].type == MediaType.IMAGE
    assert atts[0].url == "https://example.com/photo.jpg"


@pytest.mark.asyncio
async def test_middleware_adds_attachments_from_raw():
    mw = MediaNormalizerMiddleware()
    msg = _make_msg(
        raw={
            "attachments": [
                {
                    "url": "https://cdn.example.com/video.mp4",
                    "content_type": "video/mp4",
                    "size": 5000000,
                }
            ]
        }
    )
    result = await mw.process(msg, _echo_handler)
    assert "attachments" in msg.metadata
    assert msg.metadata["attachments"][0].type == MediaType.VIDEO


@pytest.mark.asyncio
async def test_middleware_passthrough_text_only():
    mw = MediaNormalizerMiddleware()
    msg = _make_msg(text="hello world")
    result = await mw.process(msg, _echo_handler)
    assert "attachments" not in msg.metadata
    assert result == "echo"


@pytest.mark.asyncio
async def test_middleware_skips_oversized_attachments():
    mw = MediaNormalizerMiddleware(max_size=1000)
    msg = _make_msg(
        raw={
            "attachments": [
                {
                    "url": "https://cdn.example.com/big.zip",
                    "content_type": "application/zip",
                    "size": 5000,
                },
                {
                    "url": "https://cdn.example.com/small.jpg",
                    "content_type": "image/jpeg",
                    "size": 500,
                },
            ]
        }
    )
    await mw.process(msg, _echo_handler)
    atts = msg.metadata["attachments"]
    assert len(atts) == 1
    assert atts[0].mime_type == "image/jpeg"


@pytest.mark.asyncio
async def test_middleware_combines_content_and_raw():
    mw = MediaNormalizerMiddleware()
    msg = _make_msg(
        content_type=ContentType.MEDIA,
        media_url="https://example.com/thumb.jpg",
        media_type="image/jpeg",
        raw={
            "attachments": [
                {"url": "https://example.com/doc.pdf", "content_type": "application/pdf"},
            ]
        },
    )
    await mw.process(msg, _echo_handler)
    assert len(msg.metadata["attachments"]) == 2


@pytest.mark.asyncio
async def test_voice_vs_audio_distinction():
    """Voice messages should remain VOICE, not collapse to AUDIO."""
    raw_voice = {"type": "voice", "mime_type": "audio/ogg", "duration": 3.0}
    att = normalize_attachment(raw_voice, "telegram")
    assert att.type == MediaType.VOICE

    raw_audio = {"mime_type": "audio/mpeg", "file_name": "song.mp3"}
    att2 = normalize_attachment(raw_audio, "telegram")
    assert att2.type == MediaType.AUDIO


# ---------------------------------------------------------------------------
# Handler helper
# ---------------------------------------------------------------------------

async def _echo_handler(msg: UnifiedMessage):
    return "echo"
