from .types import UnifiedMessage, MessageContent, Identity, ChannelStatus, ContentType, OutboundMessage, Button
from .adapter import ChannelAdapter
from .middleware import Middleware, CommandMiddleware, AccessMiddleware
from .ratelimit import RateLimitMiddleware
from .manager import ChannelManager
from .bridge import ServiceBridge
from .config import load_config
from .memory import ConversationMemory, InMemoryStore, SQLiteStore, RedisStore, MemoryStore
from .rich import RichReply
from .streaming import StreamingMiddleware, StreamingReply
from .i18n import I18nMiddleware
from .scheduler import Scheduler, parse_cron, cron_matches
from .queue import InMemoryQueue, QueueMiddleware, QueueProcessor
from .persistent_queue import SQLiteQueue, QueueItem, PersistentQueueMiddleware
from .relay import RelayMiddleware, RelayRule
from .identity import IdentityRouter

_LAZY_EXTRAS = {
    "Dashboard": ".dashboard",
    "VoiceMiddleware": ".voice",
    "STTProvider": ".voice",
    "TTSProvider": ".voice",
    "OpenAISTT": ".voice",
    "OpenAITTS": ".voice",
    "WhisperLocalSTT": ".voice",
    "Attachment": ".media",
    "MediaType": ".media",
    "MediaNormalizerMiddleware": ".media",
    "detect_media_type": ".media",
}

_LAZY_ADAPTERS = {
    "TelegramAdapter": ".adapters.telegram",
    "DiscordAdapter": ".adapters.discord",
    "SlackAdapter": ".adapters.slack",
    "LineAdapter": ".adapters.line",
    "MatrixAdapter": ".adapters.matrix",
    "MSTeamsAdapter": ".adapters.msteams",
    "FeishuAdapter": ".adapters.feishu",
    "WhatsAppAdapter": ".adapters.whatsapp",
    "IMessageAdapter": ".adapters.imessage",
    "MattermostAdapter": ".adapters.mattermost",
    "GoogleChatAdapter": ".adapters.googlechat",
    "NextcloudTalkAdapter": ".adapters.nextcloud_talk",
    "SynologyChatAdapter": ".adapters.synology_chat",
    "ZaloAdapter": ".adapters.zalo",
    "NostrAdapter": ".adapters.nostr",
    "BlueBubblesAdapter": ".adapters.bluebubbles",
    "TwitchAdapter": ".adapters.twitch",
    "IRCAdapter": ".adapters.irc",
    "WeChatAdapter": ".adapters.wechat",
    "DingTalkAdapter": ".adapters.dingtalk",
    "QQAdapter": ".adapters.qq",
    # Email / Voice / Calendar / IoT
    "EmailAdapter": ".adapters.email_imap",
    "TwilioVoiceAdapter": ".adapters.twilio_voice",
    "TwilioSMSAdapter": ".adapters.twilio_sms",
    "GoogleCalendarAdapter": ".adapters.google_calendar",
    "HomeAssistantAdapter": ".adapters.homeassistant",
    "GmailAPIAdapter": ".adapters.gmail_api",
    "OutlookAdapter": ".adapters.outlook",
    "SIPAdapter": ".adapters.sip",
    "AppleCalendarAdapter": ".adapters.apple_calendar",
}

def __getattr__(name):
    if name in _LAZY_EXTRAS:
        import importlib
        module = importlib.import_module(_LAZY_EXTRAS[name], __package__)
        return getattr(module, name)
    if name in _LAZY_ADAPTERS:
        import importlib
        module = importlib.import_module(_LAZY_ADAPTERS[name], __package__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "UnifiedMessage", "MessageContent", "Identity", "ChannelStatus", "ContentType",
    "OutboundMessage", "Button",
    "ChannelAdapter", "Middleware", "CommandMiddleware", "AccessMiddleware", "RateLimitMiddleware",
    "ChannelManager", "ServiceBridge", "load_config",
    "ConversationMemory", "InMemoryStore", "SQLiteStore", "RedisStore", "MemoryStore",
    "RichReply",
    "StreamingMiddleware", "StreamingReply",
    "I18nMiddleware",
    "Scheduler", "parse_cron", "cron_matches",
    "InMemoryQueue", "QueueMiddleware", "QueueProcessor",
    "SQLiteQueue", "QueueItem", "PersistentQueueMiddleware",
    "IdentityRouter",
    *_LAZY_EXTRAS.keys(),
    *_LAZY_ADAPTERS.keys(),
]
