"""Feishu/Lark adapter — bridges Feishu Bot to UnifiedMessage.

Requires: pip install lark-oapi aiohttp

新增能力（相对原始版本）：
  - send_card()     — 发送互动卡片消息（interactive card）
  - update_card()   — 更新已发送的卡片（用于审核流更新状态）
  - bitable_create_record() / bitable_update_record() — 多维表格读写
  - 收到消息时若包含卡片回调（interactive action），解析为 COMMAND 类型

保持向后兼容：
  - 原有 send() 接口不变（文本消息）
  - connect/disconnect/receive/get_status 接口不变
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator

from aiohttp import web
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    CreateAppTableRecordRequest,
    CreateAppTableRecordRequestBody,
    UpdateAppTableRecordRequest,
    UpdateAppTableRecordRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

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


class FeishuAdapter(ChannelAdapter):
    """Feishu/Lark channel adapter using the official lark-oapi SDK.

    Uses webhook (event subscription) for receiving and REST API for sending.

    新增方法（不影响父类接口）：
      send_card()            — 发送互动卡片，返回 message_id
      update_card()          — 更新已发卡片内容
      bitable_create_record()— 多维表格新增记录
      bitable_update_record()— 多维表格更新记录
    """

    channel_id = "feishu"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        verification_token: str = "",
        encrypt_key: str = "",
        port: int = 9000,
        path: str = "/feishu/webhook",
        command_prefix: str = "/",
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        self._port = port
        self._path = path
        self._prefix = command_prefix
        self._queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue()
        self._connected = False
        self._last_activity: datetime | None = None
        self._runner: web.AppRunner | None = None

        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .build()
        )

    # ─── 基础接口（不变） ───────────────────────────────────────────────

    async def connect(self) -> None:
        app = web.Application()
        app.router.add_post(self._path, self._handle_webhook)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        self._connected = True
        logger.info("feishu connected: webhook on port %d%s", self._port, self._path)

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        self._connected = False
        logger.info("feishu disconnected")

    async def receive(self) -> AsyncIterator[UnifiedMessage]:
        while self._connected:
            try:
                msg = await self._queue.get()
                yield msg
            except asyncio.CancelledError:
                break

    async def send(self, msg: OutboundMessage) -> str | None:
        """发送文本消息（原有接口，向后兼容）。"""
        content = json.dumps({"text": msg.text})
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(msg.chat_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )

        response = self._client.im.v1.message.create(request)
        self._last_activity = datetime.now()

        if response.success():
            return response.data.message_id if response.data else None
        logger.error("feishu send failed: %s %s", response.code, response.msg)
        return None

    async def get_status(self) -> ChannelStatus:
        return ChannelStatus(
            connected=self._connected,
            channel="feishu",
            account_id=self._app_id,
            last_activity=self._last_activity,
        )

    # ─── 新增：互动卡片 ───────────────────────────────────────────────

    async def send_card(
        self,
        chat_id: str,
        card: dict,
        *,
        receive_id_type: str = "chat_id",
    ) -> str | None:
        """发送互动卡片消息，返回 message_id。

        card 格式兼容 X-Auto fashion 工作流的卡片 payload：
          {"msg_type": "interactive", "card": {...}}  或直接传卡片 dict

        Args:
            chat_id: 目标 chat_id 或 open_id（由 receive_id_type 决定）
            card: 卡片 payload（见 X-Auto src/fashion/lark/cards.py）
            receive_id_type: "chat_id" | "open_id" | "union_id" | "user_id" | "email"
        """
        # 兼容带外层 msg_type 的格式
        card_body = card.get("card", card)
        content = json.dumps(card_body)

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )

        response = self._client.im.v1.message.create(request)
        self._last_activity = datetime.now()

        if response.success():
            message_id = response.data.message_id if response.data else None
            logger.info("feishu card sent: message_id=%s chat_id=%s", message_id, chat_id)
            return message_id
        logger.error("feishu send_card failed: %s %s", response.code, response.msg)
        return None

    async def update_card(self, message_id: str, card: dict) -> bool:
        """更新已发送的互动卡片（审核流程状态变更时调用）。

        Args:
            message_id: 原始消息 ID（om_xxx）
            card: 新的卡片 payload（格式同 send_card）

        Returns:
            True 表示更新成功
        """
        card_body = card.get("card", card)
        content = json.dumps(card_body)

        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )

        response = self._client.im.v1.message.patch(request)
        self._last_activity = datetime.now()

        if response.success():
            logger.info("feishu card updated: message_id=%s", message_id)
            return True
        logger.error("feishu update_card failed: %s %s", response.code, response.msg)
        return False

    # ─── 新增：多维表格（Bitable） ────────────────────────────────────

    async def bitable_create_record(
        self,
        app_token: str,
        table_id: str,
        fields: dict,
    ) -> str | None:
        """多维表格新增记录，返回 record_id。

        Args:
            app_token: 多维表格 token（从 URL 中获取）
            table_id: 表格 ID 或名称
            fields: 字段字典，key 为字段名，value 为字段值
        """
        request = (
            CreateAppTableRecordRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .request_body(
                CreateAppTableRecordRequestBody.builder()
                .fields(fields)
                .build()
            )
            .build()
        )

        response = self._client.bitable.v1.app_table_record.create(request)
        if response.success():
            record_id = None
            if response.data and response.data.record:
                record_id = response.data.record.record_id
            logger.info(
                "feishu bitable record created: table=%s record_id=%s",
                table_id, record_id,
            )
            return record_id
        logger.error(
            "feishu bitable_create_record failed: %s %s", response.code, response.msg
        )
        return None

    async def bitable_update_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: dict,
    ) -> bool:
        """多维表格更新记录。

        Args:
            app_token: 多维表格 token
            table_id: 表格 ID 或名称
            record_id: 记录 ID（rec_xxx）
            fields: 要更新的字段字典（仅需传修改的字段）
        """
        request = (
            UpdateAppTableRecordRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .record_id(record_id)
            .request_body(
                UpdateAppTableRecordRequestBody.builder()
                .fields(fields)
                .build()
            )
            .build()
        )

        response = self._client.bitable.v1.app_table_record.update(request)
        if response.success():
            logger.info(
                "feishu bitable record updated: record_id=%s fields=%s",
                record_id, list(fields.keys()),
            )
            return True
        logger.error(
            "feishu bitable_update_record failed: %s %s", response.code, response.msg
        )
        return False

    # ─── Webhook 处理 ─────────────────────────────────────────────────

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        body = await request.json()

        # URL verification challenge
        if body.get("type") == "url_verification":
            return web.json_response({"challenge": body.get("challenge", "")})

        # 验证 token（如已配置）
        token = body.get("token", "")
        if self._verification_token and token != self._verification_token:
            return web.Response(status=403)

        header = body.get("header", {})
        event = body.get("event", {})
        event_type = header.get("event_type", "")

        if event_type == "im.message.receive_v1":
            await self._process_message(event)
        elif event_type == "card.action.trigger":
            # 互动卡片按钮回调（飞书新版事件格式）
            await self._process_card_action(body)

        return web.json_response({"code": 0})

    async def _process_message(self, event: dict) -> None:
        sender = event.get("sender", {})
        sender_id_info = sender.get("sender_id", {})
        sender_id = sender_id_info.get("open_id", "")

        message = event.get("message", {})
        msg_id = message.get("message_id", "")
        msg_type = message.get("message_type", "")
        chat_id = message.get("chat_id", "")
        content_str = message.get("content", "{}")
        create_time = message.get("create_time", "0")

        self._last_activity = datetime.now()

        try:
            content = json.loads(content_str)
        except json.JSONDecodeError:
            content = {}

        if msg_type == "text":
            text = content.get("text", "")
            if text.startswith(self._prefix):
                parts = text[len(self._prefix):].split()
                cmd = parts[0] if parts else ""
                args = parts[1:]
                mc = MessageContent(
                    type=ContentType.COMMAND, text=text, command=cmd, args=args
                )
            else:
                mc = MessageContent(type=ContentType.TEXT, text=text)
        elif msg_type in ("image", "video", "file"):
            mc = MessageContent(type=ContentType.MEDIA, media_type=msg_type)
        else:
            return

        try:
            ts = datetime.fromtimestamp(int(create_time) / 1000)
        except (ValueError, OSError):
            ts = datetime.now()

        msg = UnifiedMessage(
            id=msg_id,
            channel="feishu",
            sender=Identity(id=sender_id),
            content=mc,
            timestamp=ts,
            chat_id=chat_id,
            raw=event,
        )
        await self._queue.put(msg)

    async def _process_card_action(self, body: dict) -> None:
        """处理互动卡片按钮点击回调，转换为 COMMAND 类型的 UnifiedMessage。

        飞书卡片 action 格式：
        {
            "header": {"event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_xxx"},
                "action": {"value": {...}, "form_value": {...}},
                "context": {"open_message_id": "om_xxx", "open_chat_id": "oc_xxx"}
            }
        }
        转换后 COMMAND：command="card_action", args=[action_value_json]
        """
        event = body.get("event", {})
        operator = event.get("operator", {})
        sender_id = operator.get("open_id", "")

        action = event.get("action", {})
        context = event.get("context", {})
        chat_id = context.get("open_chat_id", "")
        message_id = context.get("open_message_id", "")

        # 卡片按钮 value（JSON 字符串或 dict）
        action_value = action.get("value", {})
        if isinstance(action_value, str):
            try:
                action_value = json.loads(action_value)
            except json.JSONDecodeError:
                pass

        # 将 form_value（复选框选择结果）合并进 action_value
        form_value = action.get("form_value", {})
        if form_value and isinstance(action_value, dict):
            action_value = {**action_value, "form_value": form_value}

        action_json = json.dumps(action_value, ensure_ascii=False)
        self._last_activity = datetime.now()

        mc = MessageContent(
            type=ContentType.COMMAND,
            text=action_json,
            command="card_action",
            args=[action_json],
        )
        msg = UnifiedMessage(
            id=message_id,
            channel="feishu",
            sender=Identity(id=sender_id),
            content=mc,
            timestamp=datetime.now(),
            chat_id=chat_id,
            raw=body,
        )
        await self._queue.put(msg)
