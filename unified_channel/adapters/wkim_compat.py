"""WuKongIM-compatible adapter — drop-in replacement for WuKongIM server.

Implements the REST + WebSocket API that WuKongIM Android/iOS clients expect,
but routes all messages through unified-channel internally.

The client app (Android/iOS) connects to this adapter using the same
IImService interface — no client-side changes needed.

Supported endpoints:
    GET  /users/{uid}/im              → IM connection info (WebSocket IP)
    POST /conversation/sync           → sync conversations
    POST /message/channel/sync        → sync channel messages
    POST /message/sync                → sync offline messages
    POST /message/edit                → edit message (no-op ack)
    GET  /channels/{id}/{type}        → channel info
    POST /message/extra/sync          → sync extra messages
    POST /conversation/syncack        → ack conversation
    POST /message/reminder/sync       → sync reminders
    POST /message/reminder/done       → done reminders
    POST /conversation/extra/sync     → sync conversation extras
    GET  /user/sendMsg/welcome        → welcome message
    PUT  /coversation/clearUnread     → clear unread

WebSocket: /ws?uid={uid}&token={token}
    Binary frames using simplified WuKongIM-like JSON protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, AsyncIterator

from aiohttp import web

from ..adapter import ChannelAdapter
from ..types import (
    ChannelStatus,
    ContentType,
    Identity,
    MessageContent,
    OutboundMessage,
    UnifiedMessage,
)

logger = logging.getLogger(__name__)

# --- In-memory message store (replace with DB for production) ---

CS_CHANNEL_ID = "customer_service"  # Fixed channel for CS conversations
CS_CHANNEL_TYPE = 2  # Group type in WuKongIM


class MessageStore:
    """Simple in-memory message store for POC."""

    def __init__(self) -> None:
        # uid → list of messages (both sent and received)
        self.messages: dict[str, list[dict]] = defaultdict(list)
        # uid → unread count
        self.unread: dict[str, int] = defaultdict(int)
        # uid → last sync seq
        self.last_seq: dict[str, int] = defaultdict(int)
        # global message seq counter
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def add_message(self, uid: str, msg: dict) -> dict:
        msg["message_seq"] = self.next_seq()
        msg["timestamp"] = int(time.time())
        self.messages[uid].append(msg)
        return msg

    def get_messages(self, uid: str, start_seq: int = 0, limit: int = 50) -> list[dict]:
        msgs = self.messages.get(uid, [])
        return [m for m in msgs if m.get("message_seq", 0) > start_seq][:limit]

    def clear_unread(self, uid: str) -> None:
        self.unread[uid] = 0

    def incr_unread(self, uid: str) -> None:
        self.unread[uid] = self.unread.get(uid, 0) + 1


class WKIMCompatAdapter(ChannelAdapter):
    """
    WuKongIM-compatible adapter.

    Serves the same REST API and WebSocket protocol that the existing
    Android/iOS client expects. Internally converts to UnifiedMessage.

    Usage:
        adapter = WKIMCompatAdapter(host="0.0.0.0", port=8080)
        manager.add_channel(adapter)
    """

    channel_id = "wkim"

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
        ws_path: str = "/ws",
    ) -> None:
        self._host = host
        self._port = port
        self._ws_path = ws_path
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()

        # uid → WebSocket connection
        self._connections: dict[str, web.WebSocketResponse] = {}
        # uid → user info dict
        self._user_info: dict[str, dict[str, Any]] = {}
        # Message store
        self._store = MessageStore()

        self._connected = False
        self._last_activity: datetime | None = None

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    def get_user_info(self, uid: str) -> dict[str, Any] | None:
        return self._user_info.get(uid)

    async def connect(self) -> None:
        self._app = web.Application()
        self._setup_routes()

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._connected = True
        logger.info("wkim-compat listening on %s:%d", self._host, self._port)

    async def disconnect(self) -> None:
        for uid, ws in list(self._connections.items()):
            await ws.close()
        self._connections.clear()

        if self._runner:
            await self._runner.cleanup()
        self._connected = False
        logger.info("wkim-compat disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def send(self, msg: OutboundMessage) -> str | None:
        """Send a message to a connected client by uid (chat_id)."""
        uid = msg.chat_id
        ws = self._connections.get(uid)

        # Build WuKongIM-format message
        wk_msg = {
            "header": {"no_persist": 0, "red_dot": 1, "sync_once": 0},
            "setting": 0,
            "message_id": int(time.time() * 1000000),
            "message_seq": self._store.next_seq(),
            "client_msg_no": uuid.uuid4().hex,
            "from_uid": "cs_agent",
            "channel_id": CS_CHANNEL_ID,
            "channel_type": CS_CHANNEL_TYPE,
            "timestamp": int(time.time()),
            "payload": self._build_payload(msg),
        }

        # Store for sync
        self._store.add_message(uid, wk_msg)

        if ws and not ws.closed:
            # Send real-time via WebSocket
            await ws.send_json({
                "action": "recv",
                "data": wk_msg,
            })
            self._last_activity = datetime.now()
            return str(wk_msg["message_id"])
        else:
            # User offline — store for later sync
            self._store.incr_unread(uid)
            logger.info("wkim user %s offline, message stored for sync", uid)
            return str(wk_msg["message_id"])

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="wkim",
            account_id=f"{self._host}:{self._port}",
            last_activity=self._last_activity,
        )

    # --- Route setup ---

    def _setup_routes(self) -> None:
        assert self._app
        r = self._app.router

        # WebSocket
        r.add_get(self._ws_path, self._ws_handler)

        # REST API (matching IImService interface)
        r.add_get("/users/{uid}/im", self._handle_get_im_ip)
        r.add_post("/conversation/sync", self._handle_sync_chat)
        r.add_post("/message/channel/sync", self._handle_sync_channel_msg)
        r.add_post("/message/sync", self._handle_sync_msg)
        r.add_post("/message/edit", self._handle_edit_msg)
        r.add_get("/channels/{channelID}/{channelType}", self._handle_get_channel)
        r.add_post("/message/extra/sync", self._handle_sync_extra_msg)
        r.add_post("/conversation/syncack", self._handle_ack_msg)
        r.add_post("/message/reminder/sync", self._handle_sync_reminder)
        r.add_post("/message/reminder/done", self._handle_done_reminder)
        r.add_post("/conversation/extra/sync", self._handle_sync_conv_extra)
        r.add_get("/user/sendMsg/welcome", self._handle_welcome)
        r.add_put("/coversation/clearUnread", self._handle_clear_unread)

        # Health
        r.add_get("/health", self._handle_health)

    # --- WebSocket handler ---

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0, autoping=True)
        await ws.prepare(request)

        uid = request.query.get("uid", "")
        token = request.query.get("token", "")

        if not uid:
            await ws.close(message=b"uid required")
            return ws

        self._connections[uid] = ws
        self._user_info.setdefault(uid, {
            "uid": uid,
            "token": token,
            "name": request.query.get("name", uid),
            "connected_at": datetime.now().isoformat(),
        })

        logger.info("wkim client connected: uid=%s (total: %d)", uid, len(self._connections))

        # Send connect ack (WuKongIM protocol)
        await ws.send_json({
            "action": "connect_ack",
            "data": {
                "status": 1,  # success
                "server_key": "server_key_placeholder",
                "salt": "salt_placeholder",
                "time_diff": 0,
            },
        })

        try:
            async for ws_msg in ws:
                if ws_msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(ws_msg.data)
                        await self._handle_ws_message(uid, data)
                    except json.JSONDecodeError:
                        logger.debug("wkim invalid JSON from %s", uid)
                elif ws_msg.type == web.WSMsgType.BINARY:
                    # WuKongIM uses binary frames — try JSON parse
                    try:
                        data = json.loads(ws_msg.data.decode())
                        await self._handle_ws_message(uid, data)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        logger.debug("wkim unparseable binary from %s", uid)
                elif ws_msg.type == web.WSMsgType.ERROR:
                    logger.warning("wkim ws error uid=%s: %s", uid, ws.exception())
        finally:
            self._connections.pop(uid, None)
            logger.info("wkim client disconnected: uid=%s (remaining: %d)", uid, len(self._connections))

        return ws

    async def _handle_ws_message(self, uid: str, data: dict) -> None:
        action = data.get("action", "")

        if action == "ping":
            ws = self._connections.get(uid)
            if ws:
                await ws.send_json({"action": "pong"})
            return

        if action == "send":
            # Client sending a message
            payload = data.get("data", {}).get("payload", {})
            text = ""
            media_url = None
            media_type = None
            content_type = ContentType.TEXT

            # WuKongIM payload types
            wk_type = payload.get("type", 1)
            if wk_type == 1:
                # Text
                text = payload.get("content", "")
            elif wk_type == 3:
                # Image
                content_type = ContentType.MEDIA
                media_url = payload.get("url", "")
                media_type = "image"
                text = payload.get("content", "")
            elif wk_type == 4:
                # Video
                content_type = ContentType.MEDIA
                media_url = payload.get("url", "")
                media_type = "video"
                text = payload.get("content", "")
            else:
                text = payload.get("content", str(payload))

            # Build UnifiedMessage
            info = self._user_info.get(uid, {})
            content = MessageContent(
                type=content_type,
                text=text,
                media_url=media_url,
                media_type=media_type,
            )

            unified = UnifiedMessage(
                id=data.get("data", {}).get("client_msg_no", uuid.uuid4().hex[:8]),
                channel="wkim",
                sender=Identity(
                    id=uid,
                    username=uid,
                    display_name=info.get("name", uid),
                ),
                content=content,
                chat_id=uid,
                metadata={"user_info": {
                    "user_type": "authenticated",
                    "user_id": uid,
                    "name": info.get("name", uid),
                }},
                raw=data,
            )
            await self._queue.put(unified)
            self._last_activity = datetime.now()

            # Send ack back to client
            ws = self._connections.get(uid)
            if ws:
                await ws.send_json({
                    "action": "send_ack",
                    "data": {
                        "client_msg_no": data.get("data", {}).get("client_msg_no", ""),
                        "message_id": int(time.time() * 1000000),
                        "message_seq": self._store.next_seq(),
                        "status": 1,  # success
                    },
                })

            # Store the sent message
            self._store.add_message(uid, {
                "message_id": int(time.time() * 1000000),
                "from_uid": uid,
                "channel_id": CS_CHANNEL_ID,
                "channel_type": CS_CHANNEL_TYPE,
                "payload": payload,
            })

    # --- REST handlers ---

    async def _handle_get_im_ip(self, request: web.Request) -> web.Response:
        """GET /users/{uid}/im → return this server's WebSocket address."""
        uid = request.match_info["uid"]
        return web.json_response({
            "ws_addr": f"{self._host}:{self._port}",
            "tcp_addr": f"{self._host}:{self._port}",
            "uid": uid,
        })

    async def _handle_sync_chat(self, request: web.Request) -> web.Response:
        """POST /conversation/sync → return conversation list."""
        body = await self._read_json(request)
        uid = body.get("uid", "")

        unread = self._store.unread.get(uid, 0)
        return web.json_response({
            "conversations": [
                {
                    "channel_id": CS_CHANNEL_ID,
                    "channel_type": CS_CHANNEL_TYPE,
                    "unread": unread,
                    "timestamp": int(time.time()),
                    "last_msg_seq": self._store._seq,
                    "last_client_msg_no": "",
                    "version": int(time.time()),
                    "recents": self._store.get_messages(uid, limit=1)[-1:]
                    if self._store.get_messages(uid)
                    else [],
                }
            ]
        })

    async def _handle_sync_channel_msg(self, request: web.Request) -> web.Response:
        """POST /message/channel/sync → return messages for a channel."""
        body = await self._read_json(request)
        start_seq = body.get("start_message_seq", 0)
        end_seq = body.get("end_message_seq", 0)
        uid = body.get("login_uid", "")
        limit = body.get("limit", 50)

        messages = self._store.get_messages(uid, start_seq, limit)
        return web.json_response({
            "start_message_seq": start_seq,
            "end_message_seq": end_seq,
            "more": 0,
            "messages": messages,
        })

    async def _handle_sync_msg(self, request: web.Request) -> web.Response:
        """POST /message/sync → sync offline messages."""
        body = await self._read_json(request)
        uid = body.get("uid", "")
        max_seq = body.get("max_message_seq", 0)

        messages = self._store.get_messages(uid, max_seq)
        return web.json_response(messages)

    async def _handle_edit_msg(self, request: web.Request) -> web.Response:
        """POST /message/edit → ack."""
        return web.json_response({"status": 200, "msg": "ok"})

    async def _handle_get_channel(self, request: web.Request) -> web.Response:
        """GET /channels/{channelID}/{channelType} → channel info."""
        channel_id = request.match_info["channelID"]
        channel_type = int(request.match_info["channelType"])

        return web.json_response({
            "channel_id": channel_id,
            "channel_type": channel_type,
            "name": "在线客服",
            "avatar": "",
            "mute": 0,
            "top": 0,
            "online": 1,
            "last_offline": 0,
        })

    async def _handle_sync_extra_msg(self, request: web.Request) -> web.Response:
        """POST /message/extra/sync → empty list."""
        return web.json_response([])

    async def _handle_ack_msg(self, request: web.Request) -> web.Response:
        """POST /conversation/syncack → ack."""
        return web.json_response({"status": 200, "msg": "ok"})

    async def _handle_sync_reminder(self, request: web.Request) -> web.Response:
        """POST /message/reminder/sync → empty list."""
        return web.json_response([])

    async def _handle_done_reminder(self, request: web.Request) -> web.Response:
        """POST /message/reminder/done → ack."""
        return web.json_response({"status": 200, "msg": "ok"})

    async def _handle_sync_conv_extra(self, request: web.Request) -> web.Response:
        """POST /conversation/extra/sync → empty list."""
        return web.json_response([])

    async def _handle_welcome(self, request: web.Request) -> web.Response:
        """GET /user/sendMsg/welcome → welcome message."""
        return web.json_response({"status": 200, "msg": "ok"})

    async def _handle_clear_unread(self, request: web.Request) -> web.Response:
        """PUT /coversation/clearUnread → clear unread count."""
        body = await self._read_json(request)
        uid = body.get("uid", "")
        self._store.clear_unread(uid)
        return web.json_response({"status": 200, "msg": "ok"})

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "connections": len(self._connections),
        })

    # --- Helpers ---

    @staticmethod
    async def _read_json(request: web.Request) -> dict:
        try:
            return await request.json()
        except (json.JSONDecodeError, Exception):
            return {}

    @staticmethod
    def _build_payload(msg: OutboundMessage) -> dict:
        """Convert OutboundMessage to WuKongIM payload format."""
        if msg.media_url:
            media_type = msg.media_type or "image"
            if media_type in ("image", "photo"):
                return {
                    "type": 3,  # Image type in WuKongIM
                    "url": msg.media_url,
                    "content": msg.text or "",
                    "width": 0,
                    "height": 0,
                }
            elif media_type == "video":
                return {
                    "type": 4,  # Video type
                    "url": msg.media_url,
                    "content": msg.text or "",
                }
            else:
                return {
                    "type": 1,
                    "content": f"{msg.text}\n{msg.media_url}",
                }
        else:
            return {
                "type": 1,  # Text type
                "content": msg.text,
            }
