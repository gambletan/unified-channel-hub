# unified-channel

> **The missing messaging layer for AI Agents.** 18 channels, 1 unified API. Python, TypeScript, and Java.

Building an AI agent that talks to users? You need messaging. But every platform has its own SDK, auth flow, message format, and quirks. **unified-channel** gives your agent a single interface to reach users on Telegram, Discord, Slack, WhatsApp, and 14 more channels — with zero lock-in.

```
Your AI Agent
     │
     ▼
┌─────────────────────────────────────────┐
│           unified-channel               │
│                                         │
│  [Access] → [Commands] → [Your Logic]  │
│                                         │
│  Telegram │ Discord │ Slack │ WhatsApp  │
│  iMessage │ Matrix  │ Teams │ LINE      │
│  Feishu   │ Mattermost │ Google Chat   │
│  Nextcloud│ Synology│ Zalo  │ Nostr     │
│  BlueBubbles │ Twitch │ IRC            │
└─────────────────────────────────────────┘
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

- **AI Chat Agents**: Deploy your LLM-powered agent across multiple platforms from one codebase
- **Customer Support Bots**: Unified inbox across Telegram, WhatsApp, LINE, and web chat
- **DevOps Notifications**: Broadcast alerts to Slack, Discord, and Mattermost simultaneously
- **Community Management**: Single bot logic serving Discord, Telegram, and Matrix communities
- **IoT/Home Automation**: Control smart home via iMessage, Telegram, or any channel

## Project Structure

```
unified-channel/
├── python/          # Python implementation (pip install)
├── typescript/      # TypeScript/Node.js implementation (npm install)
├── java/            # Java implementation (Maven)
├── .github/
│   ├── workflows/   # CI for all three languages
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
