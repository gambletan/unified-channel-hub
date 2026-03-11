# AC Customer Support — API Reference

> AI-native omnichannel customer support.
> One QR code → any IM → instant service.

Base URL: `http://localhost:8081`

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [WebSocket (Real-time Events)](#websocket-real-time-events)
- [REST API — Tickets](#rest-api--tickets)
- [REST API — Agents](#rest-api--agents)
- [REST API — Analytics](#rest-api--analytics)
- [REST API — Connect (Customer Identity)](#rest-api--connect-customer-identity)
- [Message Schema](#message-schema)
- [Message ID Rules](#message-id-rules)
- [Topic Icon Colors (Telegram)](#topic-icon-colors-telegram)
- [ERP Integration](#erp-integration)
- [WebChat Embedding](#webchat-embedding)
- [Health Monitor](#health-monitor)
- [Configuration](#configuration)
- [Environment & Deployment](#environment--deployment)

---

## Architecture Overview

```
Customer (Telegram / WebChat / WhatsApp / LINE / WeChat / Discord / Slack)
    ↓
unified-channel (adapter layer)
    ↓
Middleware Pipeline:
  AccessMiddleware → RateLimitMiddleware → TopicBridgeMiddleware
  → IdentityMiddleware → TicketMiddleware → ConversationMemory
  → AgentReplyMiddleware → EscalationMiddleware → AI Reply
    ↓
Dashboard (port 8081) + Telegram Agent Group (forum topics)
```

All messages flow through the middleware pipeline. Each middleware can intercept, transform, or short-circuit the message before it reaches the AI reply handler.

---

## WebSocket (Real-time Events)

### Connect

```
ws://localhost:8081/ws
```

The dashboard connects via WebSocket to receive real-time ticket updates. The server pushes events; clients do not send messages.

### Event Types

| Event | Payload | Description |
|-------|---------|-------------|
| `message` | `{"type": "message", "ticket_id": "abc123"}` | New message on a ticket |
| `resolved` | `{"type": "resolved", "ticket_id": "abc123"}` | Ticket was resolved |

### Example

```javascript
const ws = new WebSocket("ws://localhost:8081/ws");
ws.onmessage = (e) => {
  const event = JSON.parse(e.data);
  if (event.type === "message") {
    // Refresh ticket messages
    fetch(`/api/tickets/${event.ticket_id}/messages`).then(/* ... */);
  }
};
```

---

## REST API — Tickets

### List Tickets

```
GET /api/tickets?status=open&channel=telegram&limit=50&offset=0
```

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `status` | string | all | `open`, `assigned`, `resolved` |
| `channel` | string | all | `telegram`, `webchat`, `whatsapp`, etc. |
| `limit` | int | 50 | Max results |
| `offset` | int | 0 | Pagination offset |

**Response** `200`:
```json
[
  {
    "id": "cae698079a5d",
    "channel": "telegram",
    "customer_name": "John",
    "subject": "Order issue",
    "status": "open",
    "priority": "normal",
    "assigned_agent_id": null,
    "created_at": "2026-03-11T06:30:00+00:00",
    "updated_at": "2026-03-11T06:35:00+00:00"
  }
]
```

### Get Ticket

```
GET /api/tickets/{id}
```

**Response** `200`:
```json
{
  "id": "cae698079a5d",
  "channel": "webchat",
  "chat_id": "guest_abc123",
  "customer_id": "2508134735381",
  "customer_name": "John",
  "subject": "Order issue",
  "status": "open",
  "priority": "normal",
  "assigned_agent_id": null,
  "language": "zh",
  "created_at": "2026-03-11T06:30:00+00:00",
  "updated_at": "2026-03-11T06:35:00+00:00",
  "resolved_at": null
}
```

**Response** `404`:
```json
{"error": "not found"}
```

### Get Ticket Messages

```
GET /api/tickets/{id}/messages
```

**Response** `200`:
```json
[
  {
    "id": 162,
    "ticket_id": "cae698079a5d",
    "role": "customer",
    "sender_name": "John",
    "content": "Where is my order?",
    "channel": "webchat",
    "from_id": "2508134735381",
    "to_id": "ai:minimax:MiniMax-Text-01",
    "created_at": "2026-03-11T07:00:20+00:00"
  },
  {
    "id": 163,
    "ticket_id": "cae698079a5d",
    "role": "ai",
    "sender_name": null,
    "content": "Let me look that up for you...",
    "channel": "webchat",
    "from_id": "ai:minimax:MiniMax-Text-01",
    "to_id": "2508134735381",
    "created_at": "2026-03-11T07:00:25+00:00"
  }
]
```

### Reply to Ticket (Agent)

```
POST /api/tickets/{id}/reply
Content-Type: application/json

{
  "text": "Your order shipped yesterday!",
  "agent_id": "alvin"
}
```

| Body Field | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `text` | string | yes | — | Reply message |
| `agent_id` | string | no | `"dashboard"` | Agent identifier |

**Response** `200`:
```json
{"ok": true}
```

The message is sent to the customer via their IM channel and stored with `from_id=agent_id, to_id=customer_id`.

### Resolve Ticket

```
POST /api/tickets/{id}/resolve
```

Resolves the ticket, releases agent load, sends a closure message to the customer, and broadcasts a `resolved` WebSocket event.

**Response** `200`:
```json
{"ok": true}
```

---

## REST API — Agents

### List Agents

```
GET /api/agents
```

**Response** `200`:
```json
[
  {
    "id": "alvin",
    "name": "Alvin",
    "status": "online",
    "current_load": 2,
    "max_concurrent": 5,
    "channel": "telegram"
  }
]
```

---

## REST API — Analytics

### Get Analytics

```
GET /api/analytics
```

Returns dashboard summary statistics (ticket counts, response times, etc.).

---

## REST API — Connect (Customer Identity)

These endpoints power the "connect" flow — linking a platform user's account to their IM identity via deep links and QR codes.

### Get Connect Links (Authenticated User)

```
GET /api/connect-links/{uid}?session_id=abc123
```

Generates personalized deep links for a platform user. Your platform backend calls this with the logged-in user's ID.

**Response** `200`:
```json
{
  "uid": "USER123",
  "links": {
    "telegram": {
      "url": "https://t.me/YourBot?start=uid_USER123",
      "name": "Telegram",
      "icon": "✈️"
    },
    "whatsapp": {
      "url": "https://wa.me/1234567890?text=uid_USER123",
      "name": "WhatsApp",
      "icon": "📱"
    },
    "webchat": {
      "url": "/chat.html?user_id=USER123",
      "name": "网页聊天 Web Chat",
      "icon": "🌐"
    }
  },
  "universal_url": "http://example.com/connect.html?uid=USER123",
  "qr_page": "/connect.html?uid=USER123"
}
```

### Get Connect Session (Anonymous WebChat)

```
GET /api/connect-session/{session_id}
```

Generates channel links for an anonymous webchat session, so the user can switch to Telegram/WhatsApp while keeping the same conversation topic.

**Response** `200`:
```json
{
  "session_id": "abc123def456",
  "links": {
    "telegram": {
      "url": "https://t.me/YourBot?start=sid_abc123def456",
      "name": "Telegram",
      "icon": "✈️"
    }
  },
  "universal_url": "http://example.com/connect.html?session_id=abc123def456",
  "qr_page": "/connect.html?session_id=abc123def456"
}
```

### Get Connect Config

```
GET /api/connect-config
```

Returns available channels for the connect landing page (no user context).

**Response** `200`:
```json
{
  "channels": [
    {"name": "Telegram", "icon": "✈️", "type": "telegram", "url": "https://t.me/YourBot", "class": "telegram"},
    {"name": "网页聊天 Web Chat", "icon": "🌐", "type": "webchat", "url": "/chat.html", "class": "webchat"}
  ]
}
```

### Get User Bindings

```
GET /api/user/{uid}/bindings
```

Shows which IM accounts are linked to a platform user.

**Response** `200`:
```json
[
  {"channel": "telegram", "chat_id": "1922559342", "bound_at": "2026-03-10T12:00:00+00:00"},
  {"channel": "webchat", "chat_id": "guest_abc123", "bound_at": "2026-03-11T06:30:00+00:00"}
]
```

---

## Message Schema

### Database Table: `ticket_messages`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `ticket_id` | TEXT | Foreign key to tickets |
| `role` | TEXT | `customer`, `ai`, `agent` |
| `sender_id` | TEXT | Legacy sender identifier |
| `sender_name` | TEXT | Display name |
| `content` | TEXT | Message body |
| `channel` | TEXT | Channel at time of message |
| `from_id` | TEXT | **Sender ID** (see rules below) |
| `to_id` | TEXT | **Receiver ID** (see rules below) |
| `created_at` | TEXT | ISO 8601 timestamp |

---

## Message ID Rules

Every message has `from_id` (sender) and `to_id` (receiver) fields that uniquely identify participants.

| Sender Type | ID Format | Example |
|-------------|-----------|---------|
| Customer | platform_user_id or chat_id | `2508134735381` |
| Guest (anonymous) | `guest_{session}` or `anon_{session}` | `guest_abc123` |
| Human agent | agent config `id` | `alvin` |
| Dashboard agent | `agent_id` from request body, default `dashboard` | `dashboard` |
| AI (MiniMax) | `ai:{backend}:{model}` | `ai:minimax:MiniMax-Text-01` |
| AI (other) | `ai:{backend}:{model}` | `ai:deepseek:deepseek-chat` |

### Message Flow Examples

```
Customer sends "Hello":
  from_id = "2508134735381"  (customer)
  to_id   = "ai:minimax:MiniMax-Text-01"

AI auto-reply:
  from_id = "ai:minimax:MiniMax-Text-01"
  to_id   = "2508134735381"  (customer)

Agent replies via Telegram topic:
  from_id = "alvin"
  to_id   = "2508134735381"  (customer)

Agent replies via dashboard:
  from_id = "dashboard"  (or specified agent_id)
  to_id   = "2508134735381"  (customer)
```

---

## Topic Icon Colors (Telegram)

New Telegram forum topics are color-coded by user type:

| User Type | Color | Value | Condition |
|-----------|-------|-------|-----------|
| Guest (anonymous) | 🔵 Blue | `7322096` | chat_id starts with `guest_` or `anon_` |
| Registered user | 🟡 Yellow | `16766590` | ERP has record, `user_level` < 1 |
| VIP user | 🔴 Red | `16478047` | ERP has record, `user_level` >= 1 |

Colors are set via the Telegram `createForumTopic` API's `icon_color` parameter. Requires ERP integration for registered/VIP detection.

---

## ERP Integration

When configured, the system queries your ERP for user info to:

1. **Color-code topics** — VIP users get red topics
2. **Inject context into AI replies** — AI sees user profile, order history
3. **Display user level** in agent dashboard

```yaml
# config.yaml
erp:
  base_url: "https://your-erp.example.com/api"
  api_key: "..."
```

The ERP client calls:
- `GET /api/users/{uid}` → user info (name, level, etc.)
- `GET /api/orders?user_id={uid}&page_size=5` → recent orders

---

## WebChat Embedding

### WebSocket Connection

```
ws://localhost:8082/ws
```

The webchat adapter runs on a separate port (default 8082). Connect via WebSocket with the standard unified-channel protocol.

### Embedding

Include the webchat widget on your site:

```html
<!-- Anonymous session -->
<script src="http://your-server:8081/chat-widget.js"></script>

<!-- Authenticated user (bind to platform account) -->
<script src="http://your-server:8081/chat-widget.js" data-user-id="USER123"></script>
```

Or link to the hosted page:
```
http://your-server:8081/chat.html?user_id=USER123
http://your-server:8081/chat.html?session_id=abc123
http://your-server:8081/chat.html  (anonymous)
```

---

## Health Monitor

An independent health check script monitors the support service and auto-recovers on failure.

### Checks (every 5 minutes)

1. **Dashboard HTTP** — `GET http://localhost:8081` → expects 200
2. **WebChat TCP** — port 8082 → expects connection
3. **Telegram Bot** — `getMe` API → expects valid response

### Failure Recovery

- **2 consecutive failures** → auto-restart via `launchctl kickstart`
- **Telegram alert** sent to admin (chat_id: `1922559342`)
- **10-minute cooldown** between restarts
- State persisted in `healthcheck_state.json`

### Alert Format

```
⚠️ AC Customer Support Service — Health Alert

Failed checks:
- dashboard_http: Connection refused
- webchat_tcp: Port 8082 not responding

Action: Auto-restarting service...
```

### Running

```bash
# Manual
python scripts/healthcheck.py

# launchd (every 5 minutes)
# ~/Library/LaunchAgents/com.tan.unified-support.healthcheck.plist
```

---

## Configuration

### config.yaml

```yaml
# Channels
channels:
  telegram:
    token: "${TELEGRAM_BOT_TOKEN}"
  webchat:
    port: 8082
  whatsapp:
    mode: unofficial
    bridge_url: "http://localhost:8084"
  # Also supported: discord, slack, wechat, line

# AI
ai:
  backend: minimax          # or: openai, deepseek, etc.
  api_key: "${AI_API_KEY}"
  base_url: "..."           # optional, for custom endpoints
  model: MiniMax-Text-01
  system_prompt: "..."      # optional
  temperature: 0.3

# Topic Bridge (Telegram agent group)
topic_bridge:
  group_chat_id: -1003828541886
  agent_ids: ["1922559342"]
  default_lang: zh
  reply_timeout: 180
  sensitive_words: ["password", "credit card"]

# Agents
agents:
  - id: alvin
    name: Alvin
    channel: telegram
    chat_id: "1922559342"
    skills: [general]

# ERP
erp:
  base_url: "https://your-erp.example.com/api"
  api_key: "..."

# Dashboard
dashboard:
  port: 8081
  host: "0.0.0.0"
  base_url: "http://192.168.1.100:8081"  # external URL for QR codes

# Rate limit
rate_limit:
  max_messages: 30
  window_seconds: 60

# Database
database:
  path: support.db

# Knowledge base (RAG)
knowledge:
  path: knowledge
  reindex_on_start: true
```

---

## Environment & Deployment

### Dependencies

```bash
pip install -e ".[all-channels]"
pip install -e ../python  # unified-channel core
```

### Start

```bash
python -m support.app                    # default config.yaml
python -m support.app /path/to/config.yaml  # custom config
```

### launchd (macOS auto-start)

```bash
# Main service — auto-start + auto-restart
# ~/Library/LaunchAgents/com.tan.unified-support.plist
launchctl load ~/Library/LaunchAgents/com.tan.unified-support.plist

# Health monitor — every 5 minutes
# ~/Library/LaunchAgents/com.tan.unified-support.healthcheck.plist
launchctl load ~/Library/LaunchAgents/com.tan.unified-support.healthcheck.plist
```

### Ports

| Service | Port | Protocol |
|---------|------|----------|
| Dashboard + REST API | 8081 | HTTP |
| WebChat | 8082 | WebSocket |
| WhatsApp Bridge | 8084 | HTTP (optional) |
