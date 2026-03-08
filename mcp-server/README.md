# unified-channel MCP Server

> Let AI agents send and receive messages across 18 channels via MCP protocol.

This MCP server wraps [unified-channel](https://github.com/gambletan/unified-channel-hub) to expose messaging capabilities as tools that any MCP-compatible AI agent can use.

## Quick Setup

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "unified-channel": {
      "command": "npx",
      "args": ["@unified-channel/mcp-server"],
      "env": {
        "UC_TELEGRAM_TOKEN": "your-telegram-bot-token",
        "UC_DISCORD_TOKEN": "your-discord-bot-token"
      }
    }
  }
}
```

### Any MCP Client

```bash
UC_TELEGRAM_TOKEN=... npx @unified-channel/mcp-server
```

## Available Tools

| Tool | Description |
|------|-------------|
| `send_message` | Send a message to a specific channel/chat |
| `broadcast_message` | Send the same message to multiple channels |
| `get_channel_status` | Check connection status of all channels |
| `list_channels` | List all 18 supported channels and their status |
| `get_recent_messages` | Get recent messages received across channels |

## Environment Variables

| Variable | Channel |
|----------|---------|
| `UC_TELEGRAM_TOKEN` | Telegram Bot API token |
| `UC_DISCORD_TOKEN` | Discord bot token |
| `UC_SLACK_BOT_TOKEN` + `UC_SLACK_APP_TOKEN` | Slack (Socket Mode) |
| `UC_MATTERMOST_URL` + `UC_MATTERMOST_TOKEN` | Mattermost |
| `UC_MATRIX_HOMESERVER` + `UC_MATRIX_TOKEN` | Matrix |

More channels coming soon. Set any combination — only configured channels are activated.

## Example Agent Interaction

```
Agent: I need to notify the team about the deployment.

→ calls list_channels
← telegram (connected), discord (connected), slack (not connected)

→ calls broadcast_message { text: "Deploy v2.1 complete ✅", targets: { telegram: "-100123", discord: "456789" } }
← telegram: sent, discord: sent

→ calls get_recent_messages { limit: 5 }
← [2026-03-08T15:30:00Z] telegram/-100123 @alice: looks good!
```

## License

MIT
