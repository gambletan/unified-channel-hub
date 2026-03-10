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
from .ai.model_router import ModelRouter
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
from .tickets.topic_bridge import TopicBridgeMiddleware

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

    # Model router (task-based LLM routing)
    ai_config = config.get("ai", {})
    model_router = ModelRouter.from_config(ai_config)
    for line in model_router.summary():
        logger.info("ModelRouter: %s", line)

    # LLM backend (for AI router, uses ai_reply task)
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

    # Optional ERP client for user info lookups (used by topic bridge + AI handler)
    erp_client = None
    erp_config = config.get("erp", {})
    if erp_config.get("base_url"):
        try:
            ac_cs_path = Path(__file__).resolve().parents[3] / "X-Auto" / "AC-Customer-Support"
            sys.path.insert(0, str(ac_cs_path))
            import erp_client as erp_mod
            erp_client = erp_mod.ERPClient(
                base_url=erp_config["base_url"],
                api_key=erp_config.get("api_key", ""),
            )
            logger.info("ERP client initialized: %s", erp_config["base_url"])
        except Exception as e:
            logger.warning("ERP client init failed (non-fatal): %s", e)

    # Topic bridge: DMs ↔ agent group forum topics
    tb_config = config.get("topic_bridge", {})
    if tb_config.get("group_chat_id"):
        from unified_channel.adapters.telegram import TelegramAdapter
        tg_adapter = manager._channels.get("telegram")
        if isinstance(tg_adapter, TelegramAdapter):
            manager.add_middleware(TopicBridgeMiddleware(
                db=db,
                tg_adapter=tg_adapter,
                group_chat_id=int(tb_config["group_chat_id"]),
                router=model_router,
                agent_ids=set(str(i) for i in tb_config.get("agent_ids", [])),
                default_lang=tb_config.get("default_lang", "zh"),
                reply_timeout=tb_config.get("reply_timeout", 180),
                sensitive_words=tb_config.get("sensitive_words"),
                erp_client=erp_client,
                send_fn=manager.send,
            ))
            logger.info("Topic bridge enabled for group %s", tb_config["group_chat_id"])

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
        # Inject ERP user/order info for authenticated users
        user_context = None
        platform_uid = (msg.metadata or {}).get("platform_user_id")
        if platform_uid and erp_client:
            try:
                parts = []
                info = await erp_client.get_user_info(platform_uid)
                if info:
                    parts.append(info.summary_for_agent())
                orders = await erp_client.get_orders(user_id=platform_uid, page_size=5)
                if orders and orders.orders:
                    parts.append(orders.summary_for_agent())
                if parts:
                    user_context = "\n\n".join(parts)
            except Exception as e:
                logger.warning("ERP context fetch failed: %s", e)
        return await ai_router.generate_reply(msg.content.text or "", formatted, user_context=user_context)

    # Dashboard
    dashboard_config = config.get("dashboard", {})
    analytics = Analytics(db)
    dashboard = DashboardAPI(
        db=db,
        analytics=analytics,
        send_fn=manager.send,
        port=dashboard_config.get("port", 8081),
        host=dashboard_config.get("host", "0.0.0.0"),
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
            elif name == "webchat":
                from unified_channel.adapters.webchat import WebChatAdapter
                manager.add_channel(WebChatAdapter(
                    port=cfg.get("port", 8082),
                    path=cfg.get("path", "/ws"),
                    cors_origins=cfg.get("cors_origins"),
                ))
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
