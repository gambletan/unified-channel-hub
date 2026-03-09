"""Main entry point — wires unified-channel + tickets + AI + dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

from unified_channel import (
    AccessMiddleware,
    ChannelManager,
    ConversationMemory,
    RateLimitMiddleware,
    UnifiedMessage,
)

from .ai.backends import create_backend
from .ai.rag import KnowledgeBase
from .ai.router import AIRouter
from .analytics.metrics import Analytics
from .dashboard.api import DashboardAPI
from .db import Database
from .models import Agent, AgentStatus
from .tickets.escalation import EscalationMiddleware
from .tickets.manager import TicketMiddleware
from .agents.pool import AgentReplyMiddleware
from .tickets.identity import IdentityMiddleware

logger = logging.getLogger(__name__)


def load_support_config(path: str = "config.yaml") -> dict:
    """Load config with ${ENV} interpolation."""
    if not Path(path).exists():
        return {}
    text = Path(path).read_text()
    # Simple env var interpolation
    import re
    def _replace(m):
        var = m.group(1)
        parts = var.split(":-", 1)
        return os.getenv(parts[0], parts[1] if len(parts) > 1 else "")
    text = re.sub(r'\$\{([^}]+)\}', _replace, text)
    return yaml.safe_load(text) or {}


async def run(config_path: str = "config.yaml") -> None:
    """Start the entire support system."""
    config = load_support_config(config_path)

    # Database
    db_path = config.get("database", {}).get("path", "support.db")
    db = Database(db_path)
    await db.connect()

    # Knowledge base
    kb_dir = config.get("knowledge", {}).get("path", "knowledge")
    kb = KnowledgeBase(db, kb_dir)
    if config.get("knowledge", {}).get("reindex_on_start", True):
        await kb.reindex()

    # LLM backend
    ai_config = config.get("ai", {})
    llm = create_backend(
        backend_name=ai_config.get("backend", "openai"),
        api_key=ai_config.get("api_key"),
        base_url=ai_config.get("base_url"),
        model=ai_config.get("model"),
    )

    # AI router
    ai_router = AIRouter(
        llm=llm,
        kb=kb,
        system_prompt=ai_config.get("system_prompt"),
        max_ai_turns=config.get("escalation", {}).get("max_ai_turns", 8),
        temperature=ai_config.get("temperature", 0.3),
    )

    # Channel manager (use unified-channel's load_config or manual setup)
    manager = ChannelManager()

    # Add channels from config
    channels_config = config.get("channels", {})
    _setup_channels(manager, channels_config)

    # Register agents
    agents_config = config.get("agents", [])
    for a in agents_config:
        agent = Agent(
            id=a["id"], name=a["name"], email=a.get("email"),
            channel=a.get("channel"), chat_id=str(a.get("chat_id", "")),
            status=AgentStatus.ONLINE,
            skills=a.get("skills", []),
        )
        await db.upsert_agent(agent)

    # Middleware pipeline (order matters!)
    admin_ids = config.get("access", {}).get("admin_ids", [])
    if admin_ids:
        manager.add_middleware(AccessMiddleware(allowed_user_ids=set(str(i) for i in admin_ids)))

    manager.add_middleware(RateLimitMiddleware(
        max_messages=config.get("rate_limit", {}).get("max_messages", 30),
        window_seconds=config.get("rate_limit", {}).get("window_seconds", 60),
    ))
    manager.add_middleware(IdentityMiddleware(db))  # Bind IM users to platform users
    manager.add_middleware(TicketMiddleware(db))
    manager.add_middleware(ConversationMemory(max_turns=20))
    manager.add_middleware(AgentReplyMiddleware(db, send_fn=manager.send))
    manager.add_middleware(EscalationMiddleware(db, ai_router, send_fn=manager.send))

    # Fallback: AI handles non-escalated messages
    @manager.on_message
    async def handle(msg: UnifiedMessage) -> str:
        history = (msg.metadata or {}).get("history", [])
        formatted = [
            {"role": h.get("role", "user"), "content": h.get("content", "")}
            for h in history[-10:]  # Last 10 turns for context
        ]
        return await ai_router.generate_reply(msg.content.text or "", formatted)

    # Dashboard
    dashboard_config = config.get("dashboard", {})
    analytics = Analytics(db)
    dashboard = DashboardAPI(
        db=db,
        analytics=analytics,
        send_fn=manager.send,
        port=dashboard_config.get("port", 8081),
    )
    await dashboard.start()

    # Run
    logger.info("unified-support starting...")
    try:
        await manager.run()
    finally:
        await dashboard.stop()
        await db.close()


def _setup_channels(manager: ChannelManager, channels_config: dict) -> None:
    """Set up channels from config dict."""
    for name, cfg in channels_config.items():
        try:
            if name == "telegram":
                from unified_channel import TelegramAdapter
                manager.add_channel(TelegramAdapter(token=cfg["token"]))
            elif name == "discord":
                from unified_channel import DiscordAdapter
                manager.add_channel(DiscordAdapter(token=cfg["token"]))
            elif name == "slack":
                from unified_channel import SlackAdapter
                manager.add_channel(SlackAdapter(
                    bot_token=cfg["bot_token"], app_token=cfg["app_token"]
                ))
            elif name == "whatsapp":
                from unified_channel.adapters.whatsapp import WhatsAppAdapter
                manager.add_channel(WhatsAppAdapter(
                    phone_number_id=cfg["phone_number_id"],
                    access_token=cfg["access_token"],
                    verify_token=cfg.get("verify_token", ""),
                ))
            elif name == "wechat":
                from unified_channel.adapters.wechat import WeChatWorkAdapter
                manager.add_channel(WeChatWorkAdapter(
                    corp_id=cfg["corp_id"],
                    agent_id=cfg["agent_id"],
                    secret=cfg["secret"],
                    token=cfg["token"],
                    encoding_aes_key=cfg["encoding_aes_key"],
                ))
            elif name == "line":
                from unified_channel.adapters.line import LineAdapter
                manager.add_channel(LineAdapter(
                    channel_secret=cfg["channel_secret"],
                    channel_access_token=cfg["channel_access_token"],
                ))
            else:
                logger.warning("Unknown channel: %s (skipping)", name)
        except Exception as e:
            logger.error("Failed to setup channel %s: %s", name, e)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    asyncio.run(run(config_path))


if __name__ == "__main__":
    main()
