# unified-channel

> **The missing messaging layer for AI Agents.** 18 channels, 1 unified API. Python, TypeScript, Java, and MCP Server.

Turn any service into something you can manage from your phone. Deploy, restart, check logs, view metrics — all from a Telegram chat (or Discord, Slack, WhatsApp, and 14 more channels). No custom bot framework needed. Just expose your functions and go.

**Also great for**: AI agent messaging, customer support bots, DevOps alerts, and anything else that needs a multi-platform messaging layer.

**New: MCP Server** — AI agents (Claude, GPT, local LLMs) can also control your services and send messages via standard MCP tool calls.

## Core Value: A Remote Control Panel in Your Pocket

**Any Service + unified-channel = full remote management via IM.**

You have a service running somewhere — a deployment pipeline, a monitoring stack, a home automation system, a GPU cluster. You want to check on it, restart things, view logs, run commands — but you're on your phone, on a train, at dinner.

With unified-channel's `ServiceBridge`, you expose your service functions as chat commands in 4 lines:

```python
from unified_channel import ChannelManager, ServiceBridge
from unified_channel.adapters.telegram import TelegramAdapter

manager = ChannelManager()
manager.add_channel(TelegramAdapter("BOT_TOKEN"))

bridge = ServiceBridge(manager)
bridge.expose("deploy", deploy_service, description="Deploy service")
bridge.expose("logs", get_logs, description="View logs")
bridge.expose("restart", restart_service, description="Restart service")
bridge.expose("metrics", get_metrics, description="View metrics")

await manager.run()
```

Now from your phone (Telegram, Discord, wherever):

```
/deploy prod v2.1     → ✅ deployed to prod
/logs api --tail 50   → [last 50 log lines]
/restart worker-3     → ✅ restarted
/metrics              → CPU: 23% | Mem: 4.2GB | QPS: 1.2k
```

**This is not a chatbot framework.** It's a remote control plane for your services that happens to use IM as the transport. Your phone becomes a terminal to anything you can write a Python/TypeScript/Java function for.

And because it supports MCP, AI agents can control your services too — same exposed functions, accessible as MCP tools.

```
┌──────────────────────────────────────────────┐
│               unified-channel                │
│                                              │
│  Your Phone (IM)  ──→  ServiceBridge  ──→  Your Service
│  AI Agent (MCP)   ──→  MCP Server    ──→  Functions
│                                              │
│  Telegram │ Discord │ Slack │ WhatsApp       │
│  iMessage │ Matrix  │ Teams │ LINE           │
│  Feishu   │ Mattermost │ Google Chat         │
│  Nextcloud│ Synology│ Zalo  │ Nostr          │
│  BlueBubbles │ Twitch │ IRC                  │
└──────────────────────────────────────────────┘
```

## Why unified-channel?

- **Built for AI agents**: Your agent logic stays clean. Channel adapters handle the messy platform details.
- **Middleware pipeline**: Access control, command routing, logging — plug in what you need, skip what you don't.
- **18 channels**: From enterprise (Slack, Teams, Feishu) to consumer (Telegram, WhatsApp, iMessage) to niche (Nostr, IRC, Synology).
- **Zero required dependencies**: Only install the SDKs for channels you actually use.
- **3 languages**: Python, TypeScript, and Java — same architecture, same API shape.

## Quick Start

### Python

```bash
pip install unified-channel[telegram]
```

```python
from unified_channel import ChannelManager, CommandMiddleware
from unified_channel.adapters.telegram import TelegramAdapter

manager = ChannelManager()
manager.add_channel(TelegramAdapter("BOT_TOKEN"))

commands = CommandMiddleware()

@commands.command("status")
async def status(msg):
    return "Agent is running!"

manager.add_middleware(commands)
manager.on_message(lambda msg: f"Echo: {msg.content.text}")

await manager.run()
```

### TypeScript

```bash
npm install unified-channel grammy
```

```typescript
import { ChannelManager, CommandMiddleware } from "unified-channel";
import { TelegramAdapter } from "unified-channel/adapters/telegram";

const manager = new ChannelManager();
manager.addChannel(new TelegramAdapter("BOT_TOKEN"));

const commands = new CommandMiddleware();
commands.command("status", async () => "Agent is running!");

manager.addMiddleware(commands).onMessage(async (msg) => `Echo: ${msg.content.text}`);

await manager.run();
```

### Java

```xml
<dependency>
  <groupId>io.github.gambletan</groupId>
  <artifactId>unified-channel</artifactId>
  <version>0.1.0</version>
</dependency>
```

```java
var manager = new ChannelManager();
manager.addChannel(new TelegramAdapter("BOT_TOKEN"));

var commands = new CommandMiddleware();
commands.command("status", msg -> HandlerResult.text("Agent is running!"));

manager.addMiddleware(commands);
manager.onMessage(msg -> HandlerResult.text("Echo: " + msg.content().text()));
manager.run();
```

## Architecture

```
Incoming Message → [Middleware 1] → [Middleware 2] → ... → [Fallback Handler]
                                                                  ↓
                              Adapter.send() ← reply (string or OutboundMessage)
```

**Core concepts:**
- **ChannelAdapter**: One per platform. Handles connect/disconnect, message parsing, sending.
- **Middleware**: Intercepts messages before your handler. Chain them for access control, command routing, logging, rate limiting.
- **ChannelManager**: Orchestrates everything. Register adapters, add middleware, set your handler, call `run()`.

## Supported Channels

| Channel | Protocol | Python | TypeScript | Java |
|---------|----------|--------|------------|------|
| Telegram | Bot API (polling) | ✅ | ✅ | ✅ |
| Discord | Gateway WebSocket | ✅ | ✅ | ✅ |
| Slack | Socket Mode | ✅ | ✅ | ✅ |
| WhatsApp | Cloud API / whatsapp-web.js | ✅ | ✅ | stub |
| iMessage | macOS SQLite + AppleScript | ✅ | ✅ | stub |
| Matrix | Client-Server API | ✅ | ✅ | stub |
| MS Teams | Bot Framework | ✅ | ✅ | stub |
| LINE | Messaging API | ✅ | ✅ | stub |
| Feishu/Lark | Event Subscription | ✅ | ✅ | stub |
| Mattermost | WebSocket + REST | ✅ | ✅ | ✅ |
| Google Chat | Service Account | ✅ | ✅ | stub |
| Nextcloud Talk | REST polling | ✅ | ✅ | stub |
| Synology Chat | Webhook | ✅ | ✅ | stub |
| Zalo | OA API | ✅ | ✅ | stub |
| Nostr | NIP-04 DM | ✅ | ✅ | stub |
| BlueBubbles | REST polling | ✅ | ✅ | stub |
| Twitch | IRC/TMI | ✅ | ✅ | stub |
| IRC | Raw IRC | ✅ | ✅ | ✅ |

## Use Cases

- **Remote Service Management** (primary): Manage deployments, view logs, restart services, check metrics — all from your phone via Telegram/Discord/Slack. `ServiceBridge` turns any set of functions into a remote control panel.
- **GPU Cluster / ML Ops**: Monitor training jobs, check GPU utilization, cancel runs, pull results — from a chat on your phone.
- **IoT / Home Automation**: Control smart home devices, check sensor readings, trigger routines via iMessage, Telegram, or any channel.
- **AI Chat Agents**: Deploy your LLM-powered agent across multiple platforms from one codebase.
- **DevOps Alerts**: Broadcast CI/CD, monitoring, and incident alerts to Slack, Discord, and Mattermost simultaneously.
- **Customer Support Bots**: Unified inbox across Telegram, WhatsApp, LINE, and web chat.
- **Community Management**: Single bot logic serving Discord, Telegram, and Matrix communities.

## MCP Server (for AI Agents)

Any MCP-compatible agent can control messaging channels as tools:

```json
{
  "mcpServers": {
    "unified-channel": {
      "command": "npx",
      "args": ["@unified-channel/mcp-server"],
      "env": {
        "UC_TELEGRAM_TOKEN": "your-bot-token",
        "UC_DISCORD_TOKEN": "your-bot-token"
      }
    }
  }
}
```

Available tools: `send_message`, `broadcast_message`, `get_channel_status`, `list_channels`, `get_recent_messages`

See [mcp-server/README.md](mcp-server/README.md) for full docs.

## Project Structure

```
unified-channel/
├── python/          # Python implementation (pip install)
├── typescript/      # TypeScript/Node.js implementation (npm install)
├── java/            # Java implementation (Maven)
├── mcp-server/      # MCP server for AI agents
├── .github/
│   ├── workflows/   # CI for all languages
│   └── PULL_REQUEST_TEMPLATE.md
└── CONTRIBUTING.md
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. We welcome PRs for:
- New channel adapters (especially Java stubs → full implementations)
- Middleware additions (rate limiting, logging, i18n)
- Bug fixes and documentation improvements
- Language-specific improvements

## License

MIT
