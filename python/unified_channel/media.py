"""Rich media normalization — unified attachment model across all platforms."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .middleware import Handler, Middleware
from .types import ContentType, UnifiedMessage

logger = logging.getLogger(__name__)


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACT = "contact"


# Mapping from MIME type prefix to MediaType
_MIME_PREFIX_MAP: dict[str, MediaType] = {
    "image": MediaType.IMAGE,
    "video": MediaType.VIDEO,
    "audio": MediaType.AUDIO,
}

# Mapping from file extension to MediaType
_EXT_MAP: dict[str, MediaType] = {
    ".jpg": MediaType.IMAGE,
    ".jpeg": MediaType.IMAGE,
    ".png": MediaType.IMAGE,
    ".gif": MediaType.IMAGE,
    ".webp": MediaType.IMAGE,
    ".bmp": MediaType.IMAGE,
    ".svg": MediaType.IMAGE,
    ".mp4": MediaType.VIDEO,
    ".mov": MediaType.VIDEO,
    ".avi": MediaType.VIDEO,
    ".mkv": MediaType.VIDEO,
    ".webm": MediaType.VIDEO,
    ".mp3": MediaType.AUDIO,
    ".ogg": MediaType.AUDIO,
    ".wav": MediaType.AUDIO,
    ".flac": MediaType.AUDIO,
    ".m4a": MediaType.AUDIO,
    ".aac": MediaType.AUDIO,
    ".pdf": MediaType.DOCUMENT,
    ".doc": MediaType.DOCUMENT,
    ".docx": MediaType.DOCUMENT,
    ".xls": MediaType.DOCUMENT,
    ".xlsx": MediaType.DOCUMENT,
    ".zip": MediaType.DOCUMENT,
    ".tar": MediaType.DOCUMENT,
    ".gz": MediaType.DOCUMENT,
}


@dataclass
class Attachment:
    """Unified attachment model across all platforms."""

    type: MediaType
    url: str | None = None
    data: bytes | None = None
    filename: str | None = None
    mime_type: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    thumbnail_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def detect_media_type(
    mime_type: str | None = None,
    filename: str | None = None,
    url: str | None = None,
) -> MediaType:
    """Detect MediaType from available information.

    Priority: mime_type > filename extension > URL path extension > DOCUMENT fallback.
    """
    # 1. Try MIME type
    if mime_type:
        prefix = mime_type.split("/")[0]
        if prefix in _MIME_PREFIX_MAP:
            return _MIME_PREFIX_MAP[prefix]
        # Special MIME types
        if "sticker" in mime_type or mime_type == "application/x-tgsticker":
            return MediaType.STICKER

    # 2. Try filename extension
    if filename:
        ext = PurePosixPath(filename).suffix.lower()
        if ext in _EXT_MAP:
            return _EXT_MAP[ext]

    # 3. Try URL path extension
    if url:
        try:
            path = urlparse(url).path
            ext = PurePosixPath(path).suffix.lower()
            if ext in _EXT_MAP:
                return _EXT_MAP[ext]
        except Exception:
            pass

    # 4. Fallback
    return MediaType.DOCUMENT


def normalize_attachment(raw: dict[str, Any], channel: str) -> Attachment:
    """Convert channel-specific attachment format to unified Attachment.

    Supports common formats from Telegram, Discord, Slack, and generic payloads.
    """
    # Detect type hint from raw data
    raw_type = raw.get("type", "").lower()
    if raw_type == "location":
        return Attachment(
            type=MediaType.LOCATION,
            metadata={
                "latitude": raw.get("latitude"),
                "longitude": raw.get("longitude"),
            },
        )
    if raw_type == "contact":
        return Attachment(
            type=MediaType.CONTACT,
            metadata={
                "phone_number": raw.get("phone_number"),
                "first_name": raw.get("first_name"),
                "last_name": raw.get("last_name"),
            },
        )
    if raw_type == "voice":
        return Attachment(
            type=MediaType.VOICE,
            url=raw.get("url") or raw.get("file_url"),
            mime_type=raw.get("mime_type") or raw.get("content_type"),
            size=raw.get("file_size") or raw.get("size"),
            duration=raw.get("duration"),
            metadata={"file_id": raw.get("file_id")} if raw.get("file_id") else {},
        )
    if raw_type == "sticker":
        return Attachment(
            type=MediaType.STICKER,
            url=raw.get("url") or raw.get("file_url"),
            filename=raw.get("file_name") or raw.get("filename"),
            mime_type=raw.get("mime_type") or raw.get("content_type"),
            width=raw.get("width"),
            height=raw.get("height"),
            metadata={"file_id": raw.get("file_id")} if raw.get("file_id") else {},
        )

    # Generic / Telegram / Discord normalization
    url = raw.get("url") or raw.get("file_url") or raw.get("proxy_url")
    mime = raw.get("mime_type") or raw.get("content_type")
    fname = raw.get("file_name") or raw.get("filename")
    media_type = detect_media_type(mime_type=mime, filename=fname, url=url)

    return Attachment(
        type=media_type,
        url=url,
        filename=fname,
        mime_type=mime,
        size=raw.get("file_size") or raw.get("size"),
        width=raw.get("width"),
        height=raw.get("height"),
        duration=raw.get("duration"),
        thumbnail_url=raw.get("thumbnail_url") or raw.get("thumb_url"),
        metadata={"file_id": raw.get("file_id")} if raw.get("file_id") else {},
    )


class MediaNormalizerMiddleware(Middleware):
    """Normalize incoming media attachments to unified Attachment format.

    - Detects media type from MIME type or URL extension
    - Downloads remote media if requested (download_media=True)
    - Converts between formats if needed
    - Adds attachment info to msg.metadata["attachments"]
    """

    def __init__(self, *, download_media: bool = False, max_size: int = 50_000_000):
        self.download_media = download_media
        self.max_size = max_size

    async def process(
        self, msg: UnifiedMessage, next_handler: Handler
    ) -> Any:
        attachments: list[Attachment] = []

        # 1. Extract from msg.content if media type
        if msg.content.type == ContentType.MEDIA:
            if msg.content.media_url:
                media_type = detect_media_type(
                    mime_type=msg.content.media_type,
                    url=msg.content.media_url,
                )
                att = Attachment(
                    type=media_type,
                    url=msg.content.media_url,
                    mime_type=msg.content.media_type,
                )
                attachments.append(att)

        # 2. Extract from msg.raw if dict with known attachment keys
        if isinstance(msg.raw, dict):
            raw_attachments = msg.raw.get("attachments", [])
            for raw_att in raw_attachments:
                if isinstance(raw_att, dict):
                    att = normalize_attachment(raw_att, msg.channel)
                    if att.size is not None and att.size > self.max_size:
                        logger.warning(
                            "attachment exceeds max_size (%d > %d), skipping",
                            att.size,
                            self.max_size,
                        )
                        continue
                    attachments.append(att)

        # 3. Download media if requested
        if self.download_media and attachments:
            for att in attachments:
                if att.url and att.data is None:
                    try:
                        att.data = await self._download(att.url)
                    except Exception:
                        logger.warning("failed to download %s", att.url, exc_info=True)

        if attachments:
            msg.metadata["attachments"] = attachments

        return await next_handler(msg)

    async def _download(self, url: str) -> bytes:
        """Download media from URL. Override for custom HTTP client."""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.read()
        except ImportError:
            raise RuntimeError(
                "aiohttp is required for download_media=True. "
                "Install it with: pip install aiohttp"
            )
