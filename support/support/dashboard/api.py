"""Dashboard API — REST + WebSocket for ticket management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import aiohttp
from aiohttp import web

from ..analytics.metrics import Analytics
from ..db import Database
from ..models import TicketStatus

logger = logging.getLogger(__name__)

# Public endpoints that don't require API key
_PUBLIC_PATHS = frozenset({
    "/api/connect-links",
    "/api/connect-session",
    "/api/connect-config",
    "/ws",
})


def _is_public(path: str) -> bool:
    """Check if a request path is public (no API key needed)."""
    for p in _PUBLIC_PATHS:
        if path == p or path.startswith(p + "/"):
            return True
    # Static files
    if not path.startswith("/api/"):
        return True
    return False


@web.middleware
async def api_key_middleware(request: web.Request, handler):
    """Verify x-api-key header for protected API endpoints."""
    if _is_public(request.path):
        return await handler(request)

    api_key = os.environ.get("SUPPORT_API_KEY", "")
    if not api_key:
        # No key configured — allow all (dev mode)
        return await handler(request)

    provided = request.headers.get("x-api-key", "")
    if provided != api_key:
        return web.json_response(
            {"error": "Unauthorized", "message": "Missing or invalid x-api-key header"},
            status=401,
        )
    return await handler(request)


class DashboardAPI:
    """Web dashboard for viewing and managing support tickets."""

    def __init__(
        self,
        db: Database,
        analytics: Analytics,
        send_fn: Any = None,
        port: int = 8081,
        host: str = "127.0.0.1",
        channels_config: dict | None = None,
        base_url: str | None = None,
    ):
        self.db = db
        self.analytics = analytics
        self.send_fn = send_fn
        self.port = port
        self.host = host
        self._channels_config = channels_config or {}
        self._base_url = (base_url or "").rstrip("/")  # e.g. "http://192.168.1.100:8081"
        self._tg_bot_username: str | None = None  # resolved lazily
        self._ws_clients: list[web.WebSocketResponse] = []
        self._app = web.Application(middlewares=[api_key_middleware])
        self._runner: web.AppRunner | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        self._app.router.add_get("/api/tickets", self._list_tickets)
        self._app.router.add_get("/api/tickets/{id}", self._get_ticket)
        self._app.router.add_get("/api/tickets/{id}/messages", self._get_messages)
        self._app.router.add_post("/api/tickets/{id}/reply", self._reply_ticket)
        self._app.router.add_post("/api/tickets/{id}/resolve", self._resolve_ticket)
        self._app.router.add_get("/api/agents", self._list_agents)
        self._app.router.add_get("/api/analytics", self._get_analytics)
        self._app.router.add_get("/api/connect-links/{uid}", self._get_connect_links)
        self._app.router.add_get("/api/connect-session/{session_id}", self._get_connect_session)
        self._app.router.add_get("/api/connect-config", self._get_connect_config)
        self._app.router.add_get("/api/user/{uid}/bindings", self._get_user_bindings)
        self._app.router.add_get("/ws", self._websocket_handler)
        # Static files
        static_dir = __import__("pathlib").Path(__file__).parent / "static"
        if static_dir.exists():
            self._app.router.add_static("/", static_dir, show_index=True)

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        # Pre-resolve Telegram bot username for QR code generation
        await self._resolve_tg_bot_username()
        logger.info("Dashboard running at http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def broadcast(self, event: dict) -> None:
        """Broadcast event to all WebSocket clients."""
        data = json.dumps(event)
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)

    # ── REST Handlers ──

    async def _list_tickets(self, request: web.Request) -> web.Response:
        status = request.query.get("status")
        channel = request.query.get("channel")
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))

        status_enum = TicketStatus(status) if status else None
        tickets = await self.db.list_tickets(status_enum, channel, limit, offset)

        return web.json_response([
            {
                "id": t.id,
                "channel": t.channel,
                "customer_name": t.customer_name,
                "subject": t.subject,
                "status": t.status.value,
                "priority": t.priority.value,
                "assigned_agent_id": t.assigned_agent_id,
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            }
            for t in tickets
        ])

    async def _get_ticket(self, request: web.Request) -> web.Response:
        ticket_id = request.match_info["id"]
        ticket = await self.db.get_ticket(ticket_id)
        if not ticket:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({
            "id": ticket.id,
            "channel": ticket.channel,
            "chat_id": ticket.chat_id,
            "customer_id": ticket.customer_id,
            "customer_name": ticket.customer_name,
            "subject": ticket.subject,
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "assigned_agent_id": ticket.assigned_agent_id,
            "language": ticket.language,
            "created_at": ticket.created_at.isoformat(),
            "updated_at": ticket.updated_at.isoformat(),
            "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        })

    async def _get_messages(self, request: web.Request) -> web.Response:
        ticket_id = request.match_info["id"]
        messages = await self.db.get_messages(ticket_id)
        return web.json_response([
            {
                "id": m.id,
                "role": m.role,
                "sender_name": m.sender_name,
                "content": m.content,
                "channel": m.channel,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ])

    async def _reply_ticket(self, request: web.Request) -> web.Response:
        """Agent replies from dashboard."""
        ticket_id = request.match_info["id"]
        body = await request.json()
        text = body.get("text", "")
        if not text:
            return web.json_response({"error": "text required"}, status=400)

        ticket = await self.db.get_ticket(ticket_id)
        if not ticket:
            return web.json_response({"error": "not found"}, status=404)

        # Send to customer via unified-channel
        if self.send_fn:
            await self.send_fn(ticket.channel, ticket.chat_id, text)

        # Store message
        from ..models import TicketMessage
        agent_id = body.get("agent_id", "dashboard")
        await self.db.add_message(TicketMessage(
            ticket_id=ticket_id,
            role="agent",
            sender_id=agent_id,
            content=text,
            channel=ticket.channel,
            from_id=agent_id,
            to_id=ticket.customer_id,
        ))

        await self.broadcast({"type": "message", "ticket_id": ticket_id})
        return web.json_response({"ok": True})

    async def _resolve_ticket(self, request: web.Request) -> web.Response:
        ticket_id = request.match_info["id"]
        ticket = await self.db.get_ticket(ticket_id)
        if not ticket:
            return web.json_response({"error": "not found"}, status=404)

        await self.db.update_ticket_status(ticket_id, TicketStatus.RESOLVED)
        if ticket.assigned_agent_id:
            await self.db.update_agent_load(ticket.assigned_agent_id, -1)
        await self.db.log_event("resolved", ticket_id=ticket_id)

        if self.send_fn:
            await self.send_fn(
                ticket.channel, ticket.chat_id,
                "Your issue has been resolved. Thank you! 😊"
            )

        await self.broadcast({"type": "resolved", "ticket_id": ticket_id})
        return web.json_response({"ok": True})

    async def _list_agents(self, request: web.Request) -> web.Response:
        agents = await self.db.list_agents()
        return web.json_response([
            {
                "id": a.id,
                "name": a.name,
                "status": a.status.value,
                "current_load": a.current_load,
                "max_concurrent": a.max_concurrent,
                "channel": a.channel,
            }
            for a in agents
        ])

    async def _get_analytics(self, request: web.Request) -> web.Response:
        summary = await self.analytics.summary()
        return web.json_response(summary)

    def _resolve_base_url(self, request: web.Request) -> str:
        """Return the external base URL for generating QR codes / links."""
        if self._base_url:
            return self._base_url
        host = request.headers.get("X-Forwarded-Host", request.host)
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        return f"{scheme}://{host}"

    async def _resolve_tg_bot_username(self) -> str | None:
        """Resolve Telegram bot username from token via getMe API (cached)."""
        if self._tg_bot_username is not None:
            return self._tg_bot_username or None
        tg_cfg = self._channels_config.get("telegram", {})
        token = tg_cfg.get("token")
        if not token:
            self._tg_bot_username = ""
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.telegram.org/bot{token}/getMe", timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
                    username = data.get("result", {}).get("username", "")
                    self._tg_bot_username = username
                    logger.info("Resolved Telegram bot username: @%s", username)
                    return username or None
        except Exception as e:
            logger.warning("Failed to resolve Telegram bot username: %s", e)
            self._tg_bot_username = ""
            return None

    def _build_channel_links(self, uid: str | None = None, session_id: str | None = None) -> dict:
        """Build channel links/info from config.

        Args:
            uid: Platform user ID (authenticated user) — generates uid_xxx deep links.
            session_id: Webchat session ID (anonymous user) — generates sid_xxx deep links
                        so the new channel gets linked to the existing webchat topic.
        """
        links = {}
        cfg = self._channels_config

        # Determine the start/bind payload: uid takes priority over session_id
        if uid:
            payload = f"uid_{uid}"
        elif session_id:
            payload = f"sid_{session_id}"
        else:
            payload = None

        if "telegram" in cfg and self._tg_bot_username:
            bot = self._tg_bot_username
            if payload:
                links["telegram"] = {"url": f"https://t.me/{bot}?start={payload}", "name": "Telegram", "icon": "✈️"}
            else:
                links["telegram"] = {"url": f"https://t.me/{bot}", "name": "Telegram", "icon": "✈️"}

        if "whatsapp" in cfg:
            wa_number = cfg["whatsapp"].get("phone_number_id", "")
            if wa_number:
                url = f"https://wa.me/{wa_number}" + (f"?text={payload}" if payload else "")
                links["whatsapp"] = {"url": url, "name": "WhatsApp", "icon": "📱"}

        if "line" in cfg:
            line_bot_id = cfg["line"].get("bot_id", "")
            if line_bot_id:
                url = f"https://line.me/R/oaMessage/{line_bot_id}/" + (f"?{payload}" if payload else "")
                links["line"] = {"url": url, "name": "LINE", "icon": "🟢"}

        if "webchat" in cfg:
            if uid:
                links["webchat"] = {"url": f"/chat.html?user_id={uid}", "name": "网页聊天 Web Chat", "icon": "🌐"}
            elif session_id:
                links["webchat"] = {"url": f"/chat.html?session_id={session_id}", "name": "网页聊天 Web Chat", "icon": "🌐"}
            else:
                links["webchat"] = {"url": "/chat.html", "name": "网页聊天 Web Chat", "icon": "🌐"}

        return links

    async def _get_connect_links(self, request: web.Request) -> web.Response:
        """Generate personalized deep links for a platform user.

        Your platform calls this with the logged-in user's ID to generate
        links/QR codes that bind IM identity to the platform account.

        GET /api/connect-links/USER123
        → { uid, links: { telegram: {url, name, icon}, ... }, qr_url }
        """
        uid = request.match_info["uid"]
        session_id = request.query.get("session_id")
        await self._resolve_tg_bot_username()
        links = self._build_channel_links(uid=uid, session_id=session_id)
        base = self._resolve_base_url(request)

        return web.json_response({
            "uid": uid,
            "links": links,
            "universal_url": f"{base}/connect.html?uid={uid}",
            "qr_page": f"/connect.html?uid={uid}",
        })

    async def _get_connect_session(self, request: web.Request) -> web.Response:
        """Generate channel links for an anonymous webchat session.

        The webchat frontend calls this so the user can switch to Telegram/WhatsApp
        while keeping the same conversation topic.

        GET /api/connect-session/abc123def456
        → { session_id, links: { telegram: {url, ...}, ... }, universal_url }
        """
        session_id = request.match_info["session_id"]
        await self._resolve_tg_bot_username()
        links = self._build_channel_links(session_id=session_id)
        base = self._resolve_base_url(request)

        return web.json_response({
            "session_id": session_id,
            "links": links,
            "universal_url": f"{base}/connect.html?session_id={session_id}",
            "qr_page": f"/connect.html?session_id={session_id}",
        })

    async def _get_connect_config(self, request: web.Request) -> web.Response:
        """Return available channels for the connect landing page (no uid).

        GET /api/connect-config
        → { channels: [{name, icon, type, url}, ...] }
        """
        await self._resolve_tg_bot_username()
        links = self._build_channel_links()

        channels = []
        for ch_type, info in links.items():
            channels.append({
                "name": info["name"],
                "icon": info["icon"],
                "type": ch_type,
                "url": info["url"],
                "class": ch_type,
            })

        return web.json_response({"channels": channels})

    async def _get_user_bindings(self, request: web.Request) -> web.Response:
        """Get all channel bindings for a platform user.

        Shows which IM accounts are linked to this user.
        """
        uid = request.match_info["uid"]
        bindings = await self.db.get_bindings_by_user(uid)
        return web.json_response([
            {
                "channel": b.channel,
                "chat_id": b.chat_id,
                "bound_at": b.bound_at.isoformat(),
            }
            for b in bindings
        ])

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.append(ws)
        try:
            async for _ in ws:
                pass  # We only push, don't read
        finally:
            try:
                self._ws_clients.remove(ws)
            except ValueError:
                pass
        return ws
