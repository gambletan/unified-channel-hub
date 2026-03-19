from .types import UnifiedMessage, MessageContent, Identity, ChannelStatus, ContentType, OutboundMessage, Button
from .adapter import ChannelAdapter
from .middleware import Middleware, CommandMiddleware, AccessMiddleware
from .ratelimit import RateLimitMiddleware
from .manager import ChannelManager
from .bridge import ServiceBridge
from .config import load_config
from .memory import ConversationMemory, InMemoryStore, SQLiteStore, RedisStore, MemoryStore
from .rich import RichReply
from .queue import InMemoryQueue, QueueMiddleware, QueueProcessor

_LAZY_EXTRAS = {
    # Streaming / i18n / scheduler / persistent queue / relay / identity
    # are lazy-loaded to reduce cold import cost (~30-40% faster)
    "StreamingMiddleware": ".streaming",
    "StreamingReply": ".streaming",
    "I18nMiddleware": ".i18n",
    "Scheduler": ".scheduler",
    "parse_cron": ".scheduler",
    "cron_matches": ".scheduler",
    "SQLiteQueue": ".persistent_queue",
    "QueueItem": ".persistent_queue",
    "PersistentQueueMiddleware": ".persistent_queue",
    "RelayMiddleware": ".relay",
    "RelayRule": ".relay",
    "IdentityRouter": ".identity",
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
    # Eagerly imported
    "UnifiedMessage", "MessageContent", "Identity", "ChannelStatus", "ContentType",
    "OutboundMessage", "Button",
    "ChannelAdapter", "Middleware", "CommandMiddleware", "AccessMiddleware", "RateLimitMiddleware",
    "ChannelManager", "ServiceBridge", "load_config",
    "ConversationMemory", "InMemoryStore", "SQLiteStore", "RedisStore", "MemoryStore",
    "RichReply",
    "InMemoryQueue", "QueueMiddleware", "QueueProcessor",
    # Lazy-loaded
    *_LAZY_EXTRAS.keys(),
    *_LAZY_ADAPTERS.keys(),
]
