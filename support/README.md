# AC Customer Support Service

AI-native omnichannel customer support system — one QR code, any IM, instant service.

## Architecture

```
Customer (Telegram / WebChat / WhatsApp / ...)
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

## Database Schema

### ticket_messages

All messages (customer, AI, agent) are stored in a single table with `from_id` / `to_id` tracking.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| ticket_id | TEXT | Foreign key to tickets |
| role | TEXT | `customer`, `ai`, `agent` |
| sender_id | TEXT | Legacy sender identifier |
| sender_name | TEXT | Display name |
| content | TEXT | Message body |
| channel | TEXT | Channel at time of message |
| from_id | TEXT | Sender ID (see ID rules below) |
| to_id | TEXT | Receiver ID (see ID rules below) |
| created_at | TEXT | ISO 8601 timestamp |

### Message ID Rules

| Sender Type | ID Format | Example |
|-------------|-----------|---------|
| Customer | platform_user_id or chat_id | `2508134735381` |
| Guest (anonymous) | `guest_{session}` or `anon_{session}` | `guest_abc123` |
| Human agent | agent config `id` | `alvin` |
| Dashboard agent | `agent_id` from request, default `dashboard` | `dashboard` |
| AI (MiniMax) | `ai:{backend}:{model}` | `ai:minimax:MiniMax-Text-01` |
| AI (other) | `ai:{backend}:{model}` | `ai:deepseek:deepseek-chat` |

AI IDs follow the pattern `ai:{backend}:{model}` — each model gets a unique sender identity.

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
```

### Topic Icon Colors (Telegram)

New forum topics are color-coded by user type:

| User Type | Color | Value | Condition |
|-----------|-------|-------|-----------|
| Guest (anonymous) | Blue | `7322096` | chat_id starts with `guest_` / `anon_` |
| Registered user | Yellow | `16766590` | ERP has record, `user_level` < 1 |
| VIP user | Red | `16478047` | ERP has record, `user_level` >= 1 |

## Services

### Main App

```bash
# Start
python -m support.app

# launchd (auto-start + auto-restart)
# ~/Library/LaunchAgents/com.tan.unified-support.plist
```

- Dashboard: `http://0.0.0.0:8081`
- WebChat WebSocket: `http://0.0.0.0:8082`
- Telegram Bot: polling mode

### Health Monitor

```bash
# Manual run
python scripts/healthcheck.py

# launchd (every 5 minutes)
# ~/Library/LaunchAgents/com.tan.unified-support.healthcheck.plist
```

Checks:
1. Dashboard HTTP 8081 → 200
2. WebChat TCP 8082 → port open
3. Telegram Bot → getMe API

On failure (2 consecutive):
- Auto-restart launchd service
- Send Telegram alert to admin (chat_id: `1922559342`)
- 10-minute restart cooldown

## Configuration

Main config: `config.yaml`

```yaml
channels:
  telegram:
    token: "BOT_TOKEN"
  webchat:
    port: 8082

ai:
  backend: minimax
  api_key: "..."
  model: MiniMax-Text-01

topic_bridge:
  group_chat_id: -1003828541886
  agent_ids: ["1922559342"]

agents:
  - id: alvin
    name: Alvin
    channel: telegram
    chat_id: "1922559342"
    skills: [general]
```

## Dashboard API

Base URL: `http://localhost:8081`

### Tickets

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/tickets` | List tickets. Query: `?status=open&channel=telegram&limit=50&offset=0` |
| GET | `/api/tickets/{id}` | Get ticket detail |
| GET | `/api/tickets/{id}/messages` | Get all messages for a ticket. Response includes `from_id`, `to_id` fields |
| POST | `/api/tickets/{id}/reply` | Agent reply. Body: `{"text": "...", "agent_id": "alvin"}` |
| POST | `/api/tickets/{id}/resolve` | Resolve/close ticket |

### Agents

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List all configured agents |

### Analytics

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/analytics` | Dashboard analytics/stats |

### Connect (Customer Identity)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/connect-links/{uid}` | Get IM connect links for a platform user |
| GET | `/api/connect-session/{session_id}` | Get session info for webchat |
| GET | `/api/connect-config` | Get webchat/channel config |
| GET | `/api/user/{uid}/bindings` | Get all channel bindings for a user |

### WebSocket

| Path | Description |
|------|-------------|
| `/ws` | Real-time ticket/message events for dashboard |

### Message Response Format

```json
{
  "id": 162,
  "ticket_id": "cae698079a5d",
  "role": "ai",
  "sender_id": null,
  "sender_name": null,
  "content": "Hello! How can I help?",
  "channel": "webchat",
  "from_id": "ai:minimax:MiniMax-Text-01",
  "to_id": "2508134735381",
  "created_at": "2026-03-11T07:00:25+00:00"
}
```

## Dependencies

```bash
pip install -e ".[all-channels]"
# Also needs: pip install -e ../python  (unified-channel core)
```
