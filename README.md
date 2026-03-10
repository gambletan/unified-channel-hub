[![CI](https://github.com/gambletan/unified-channel-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/gambletan/unified-channel-hub/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/unified-channel)](https://pypi.org/project/unified-channel/)
[![npm](https://img.shields.io/npm/v/unified-channel)](https://www.npmjs.com/package/unified-channel)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Node 18+](https://img.shields.io/badge/node-18+-green.svg)](https://nodejs.org/)

# unified-channel

**The only lightweight, embeddable messaging library with 18 channels, ServiceBridge, conversation memory, rich output, and streaming — in Python, TypeScript, and Java.**

Not a chatbot platform. Not an AI agent framework. A library you `pip install` / `npm install` / Maven-add into **your** project and call `manager.run()`.

## Why This Exists

| Project | What it is | The gap |
|---------|-----------|---------|
| **OpenClaw / Nanobot** | Full AI agent frameworks | Too heavy if you just need messaging. You get an entire runtime, config system, and opinionated agent loop. |
| **Botpress / Hexabot** | Chatbot platforms | Not embeddable. You build inside *their* platform, not yours. |
| **LangBot** | AI-to-IM bridge | No ServiceBridge (remote function control), no conversation memory, no rich output, no streaming. |
| **unified-channel** | **Lightweight library you embed in YOUR project** | That's the point — there is no gap. |

unified-channel gives you the messaging plumbing so you can focus on your actual logic: service management, AI agents, alerting, customer support, community bots — whatever.

## Feature Highlights

| Feature | What it does |
|---------|-------------|
| **18 Channels** | Telegram, Discord, Slack, WhatsApp, iMessage, Matrix, Teams, LINE, Feishu, Mattermost, Google Chat, Nextcloud, Synology, Zalo, Nostr, BlueBubbles, Twitch, IRC |
| **ServiceBridge** | Expose any function as a chat command. Your phone becomes a remote control for your services. |
| **ConversationMemory** | Per-user/per-channel conversation history. In-memory, SQLite, or Redis backends. |
| **RichReply** | Send buttons, carousels, images, files — auto-degrades gracefully on platforms that don't support them. |
| **Streaming** | Typing indicators + chunked message delivery for long-running responses. |
| **MCP Server** | AI agents (Claude, GPT, local LLMs) control your services via standard MCP tool calls. |
| **3 Languages** | Python, TypeScript, Java — same architecture, same API shape. |

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

## Conversation Memory

Track per-user, per-channel conversation history with pluggable backends:

```python
from unified_channel import ChannelManager, ConversationMemory
from unified_channel.memory import SQLiteBackend

manager = ChannelManager()
memory = ConversationMemory(backend=SQLiteBackend("conversations.db"))
manager.add_middleware(memory)

@manager.on_message
async def handle(msg):
    history = await memory.get_history(msg.sender_id, limit=10)
    # Pass history to your LLM, search engine, or custom logic
    return generate_response(msg.content.text, history)
```

Backends: `InMemoryBackend` (default), `SQLiteBackend`, `RedisBackend`. Each stores messages with sender, channel, timestamp, and metadata.

## Rich Reply

Send structured content that auto-degrades across platforms:

```python
from unified_channel import RichReply

reply = (
    RichReply("Here are your options:")
    .add_buttons(["Approve", "Reject", "Defer"])
    .add_image("https://example.com/chart.png", alt="CPU usage graph")
    .set_footer("Reply within 24h")
)
return reply
```

On Telegram: inline keyboard buttons + image. On Slack: Block Kit. On IRC: plain text with numbered options. Each adapter maps rich elements to the best available platform primitive — no manual per-platform logic.

## Streaming

Long-running responses get typing indicators and chunked delivery instead of awkward silence:

```python
from unified_channel import StreamingReply

async def handle(msg):
    stream = StreamingReply(msg)
    await stream.start_typing()

    async for chunk in call_llm_streaming(msg.content.text):
        await stream.send_chunk(chunk)

    await stream.finish()
```

On platforms that support edit-in-place (Telegram, Discord, Slack): the message updates live. On others: chunks are batched and sent at reasonable intervals. Typing indicators are sent automatically during gaps.

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

## Feature Comparison

| Feature | unified-channel | OpenClaw | Botpress | LangBot |
|---------|:-:|:-:|:-:|:-:|
| Embeddable library | **Yes** | No (full agent) | No (platform) | Partial |
| Channel count | **18** | 18 | 4 | 10 |
| ServiceBridge (remote control) | **Yes** | No | No | No |
| Conversation memory | **Yes** | Yes | Yes | No |
| Rich reply (auto-degrade) | **Yes** | Partial | Yes | No |
| Streaming output | **Yes** | Yes | No | No |
| MCP Server | **Yes** | No | No | No |
| Python + TypeScript + Java | **Yes** | TS only | TS only | Python only |
| Zero required deps | **Yes** | No | No | No |

## Supported Channels

| Channel | Protocol | Python | TypeScript | Java |
|---------|----------|--------|------------|------|
| Telegram | Bot API (polling) | Yes | Yes | Yes |
| Discord | Gateway WebSocket | Yes | Yes | Yes |
| Slack | Socket Mode | Yes | Yes | Yes |
| WhatsApp | Cloud API / whatsapp-web.js | Yes | Yes | stub |
| iMessage | macOS SQLite + AppleScript | Yes | Yes | stub |
| Matrix | Client-Server API | Yes | Yes | stub |
| MS Teams | Bot Framework | Yes | Yes | stub |
| LINE | Messaging API | Yes | Yes | stub |
| Feishu/Lark | Event Subscription | Yes | Yes | stub |
| Mattermost | WebSocket + REST | Yes | Yes | Yes |
| Google Chat | Service Account | Yes | Yes | stub |
| Nextcloud Talk | REST polling | Yes | Yes | stub |
| Synology Chat | Webhook | Yes | Yes | stub |
| Zalo | OA API | Yes | Yes | stub |
| Nostr | NIP-04 DM | Yes | Yes | stub |
| BlueBubbles | REST polling | Yes | Yes | stub |
| Twitch | IRC/TMI | Yes | Yes | stub |
| IRC | Raw IRC | Yes | Yes | Yes |

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
- New channel adapters (especially Java stubs to full implementations)
- Middleware additions (rate limiting, logging, i18n)
- Bug fixes and documentation improvements
- Language-specific improvements

## License

MIT
