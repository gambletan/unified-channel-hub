"""Identity binding — maps IM users to your platform users.

Flow:
1. Your platform generates a personalized link for the logged-in user:
   - Telegram: https://t.me/your_bot?start=uid_12345
   - WeChat: QR code with scene_id=uid_12345
   - WhatsApp: https://wa.me/number?text=uid_12345
   - LINE: liff deep link with uid param
   - Generic: https://your-domain/connect?uid=12345 (landing page)

2. When the user clicks/scans, the bot receives:
   - Telegram: /start uid_12345
   - WeChat: event with scene_id
   - WhatsApp: first message "uid_12345"
   - The middleware intercepts this and binds channel_id → platform_user_id

3. All subsequent messages from this chat are tagged with the platform user ID.
   The support agent sees: "Alice (user #12345)" instead of "Telegram user 9876543".
"""

from __future__ import annotations

import logging
import re
from typing import Any

from unified_channel import Middleware, UnifiedMessage

from ..db import Database

logger = logging.getLogger(__name__)

Handler = Any

# Pattern to extract platform user ID from /start or first message
# Supports: uid_xxx, uid:xxx, uid=xxx, or just a raw UUID/numeric ID after /start
UID_PATTERNS = [
    re.compile(r"^/start\s+uid[_:=](\S+)", re.IGNORECASE),
    re.compile(r"^uid[_:=](\S+)", re.IGNORECASE),  # WhatsApp/LINE first message
    re.compile(r"^bind\s+(\S+)", re.IGNORECASE),  # Explicit /bind command
]

# Pattern to extract webchat session ID for cross-channel linking
# e.g. /start sid_abc123 → link this Telegram chat to webchat session abc123
SID_PATTERNS = [
    re.compile(r"^/start\s+sid[_:=](\S+)", re.IGNORECASE),
    re.compile(r"^sid[_:=](\S+)", re.IGNORECASE),
]


class IdentityMiddleware(Middleware):
    """Binds IM users to your platform users via deep links.

    Usage in your platform:
        Generate a link like: https://t.me/your_bot?start=uid_USER123
        When user clicks it, their Telegram account gets bound to USER123.

    After binding, every ticket from this chat shows the platform user ID,
    so agents know exactly who they're talking to.
    """

    def __init__(self, db: Database, welcome_msg: str | None = None):
        self.db = db
        self.welcome_msg = welcome_msg or (
            "Welcome! Your account has been linked. "
            "You can now chat with our support team. How can we help? 😊"
        )

    async def process(self, msg: UnifiedMessage, next_handler: Handler) -> Any:
        text = (msg.content.text or "").strip()
        channel = msg.channel
        chat_id = msg.chat_id or msg.sender.id

        # Try to extract session ID for cross-channel linking (sid_xxx)
        session_id = self._extract_session_id(text)
        if session_id:
            # Link this new channel to the existing webchat session's topic
            if not hasattr(msg, "metadata") or msg.metadata is None:
                msg.metadata = {}
            msg.metadata["link_session_id"] = session_id
            msg.metadata["link_channel"] = channel
            msg.metadata["link_chat_id"] = chat_id
            logger.info(
                "Cross-channel link: %s:%s → webchat session %s",
                channel, chat_id, session_id,
            )
            return await next_handler(msg)

        # Try to extract platform user ID from the message
        platform_uid = self._extract_uid(text)

        if platform_uid:
            # Bind this channel identity to the platform user
            await self.db.bind_customer(
                platform_user_id=platform_uid,
                channel=channel,
                chat_id=chat_id,
                metadata={
                    "sender_id": msg.sender.id,
                    "sender_name": msg.sender.display_name or msg.sender.username,
                },
            )
            logger.info(
                "Bound %s:%s → platform user %s",
                channel, chat_id, platform_uid,
            )
            return self.welcome_msg

        # Look up existing binding and inject into metadata
        binding = await self.db.get_binding_by_chat(channel, chat_id)
        if binding:
            if not hasattr(msg, "metadata") or msg.metadata is None:
                msg.metadata = {}
            msg.metadata["platform_user_id"] = binding.platform_user_id
            msg.metadata["binding"] = binding

        return await next_handler(msg)

    def _extract_uid(self, text: str) -> str | None:
        """Try to extract a platform user ID from message text."""
        for pattern in UID_PATTERNS:
            m = pattern.match(text)
            if m:
                return m.group(1)
        return None

    def _extract_session_id(self, text: str) -> str | None:
        """Try to extract a webchat session ID (sid_xxx) from message text."""
        for pattern in SID_PATTERNS:
            m = pattern.match(text)
            if m:
                return m.group(1)
        return None
