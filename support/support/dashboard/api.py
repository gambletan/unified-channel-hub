"""Dashboard API — REST + WebSocket for ticket management."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from ..analytics.metrics import Analytics
from ..db import Database
from ..models import TicketStatus

logger = logging.getLogger(__name__)


class DashboardAPI:
    """Web dashboard for viewing and managing support tickets."""

    def __init__(
        self,
        db: Database,
        analytics: Analytics,
        send_fn: Any = None,
        port: int = 8081,
        host: str = "127.0.0.1",
    ):
        self.db = db
        self.analytics = analytics
        self.send_fn = send_fn
        self.port = port
        self.host = host
        self._ws_clients: list[web.WebSocketResponse] = []
        self._app = web.Application()
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
        await self.db.add_message(TicketMessage(
            ticket_id=ticket_id,
            role="agent",
            content=text,
            channel=ticket.channel,
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

    async def _get_connect_links(self, request: web.Request) -> web.Response:
        """Generate personalized deep links for a platform user.

        Your platform calls this with the logged-in user's ID to generate
        links/QR codes that bind IM identity to the platform account.

        GET /api/connect-links/USER123
        → { telegram: "https://t.me/bot?start=uid_USER123", ... }
        """
        uid = request.match_info["uid"]
        # Build links for all configured channels
        links = {}
        # These would be configured per-deployment; here are templates
        bot_configs = {
            "telegram": request.query.get("tg_bot"),
            "whatsapp": request.query.get("wa_number"),
            "line": request.query.get("line_id"),
        }
        if bot_configs.get("telegram"):
            links["telegram"] = f"https://t.me/{bot_configs['telegram']}?start=uid_{uid}"
        if bot_configs.get("whatsapp"):
            links["whatsapp"] = f"https://wa.me/{bot_configs['whatsapp']}?text=uid_{uid}"
        if bot_configs.get("line"):
            links["line"] = f"https://line.me/R/oaMessage/{bot_configs['line']}/?uid_{uid}"

        # Universal landing page link
        links["universal"] = f"/connect.html?uid={uid}"

        return web.json_response({"uid": uid, "links": links})

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
