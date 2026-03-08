#!/usr/bin/env node

/**
 * unified-channel MCP Server
 *
 * Exposes messaging channels as MCP tools for AI agents.
 * Agents can send messages, check channel status, list channels,
 * and receive messages through any of the 18 supported platforms.
 *
 * Usage:
 *   npx @unified-channel/mcp-server
 *
 * Configure channels via environment variables or config file.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

// Channel configuration from environment
interface ChannelConfig {
  type: string;
  enabled: boolean;
  config: Record<string, string>;
}

// In-memory message buffer for received messages
interface BufferedMessage {
  id: string;
  channel: string;
  sender: string;
  text: string;
  timestamp: string;
  chatId: string;
}

const messageBuffer: BufferedMessage[] = [];
const MAX_BUFFER = 100;

// Parse channel configs from environment
function parseChannelConfigs(): ChannelConfig[] {
  const configs: ChannelConfig[] = [];

  // Telegram
  if (process.env.UC_TELEGRAM_TOKEN) {
    configs.push({
      type: "telegram",
      enabled: true,
      config: { token: process.env.UC_TELEGRAM_TOKEN },
    });
  }

  // Discord
  if (process.env.UC_DISCORD_TOKEN) {
    configs.push({
      type: "discord",
      enabled: true,
      config: { token: process.env.UC_DISCORD_TOKEN },
    });
  }

  // Slack
  if (process.env.UC_SLACK_BOT_TOKEN && process.env.UC_SLACK_APP_TOKEN) {
    configs.push({
      type: "slack",
      enabled: true,
      config: {
        botToken: process.env.UC_SLACK_BOT_TOKEN,
        appToken: process.env.UC_SLACK_APP_TOKEN,
      },
    });
  }

  // Mattermost
  if (process.env.UC_MATTERMOST_URL && process.env.UC_MATTERMOST_TOKEN) {
    configs.push({
      type: "mattermost",
      enabled: true,
      config: {
        url: process.env.UC_MATTERMOST_URL,
        token: process.env.UC_MATTERMOST_TOKEN,
      },
    });
  }

  // Matrix
  if (process.env.UC_MATRIX_HOMESERVER && process.env.UC_MATRIX_TOKEN) {
    configs.push({
      type: "matrix",
      enabled: true,
      config: {
        homeserver: process.env.UC_MATRIX_HOMESERVER,
        token: process.env.UC_MATRIX_TOKEN,
      },
    });
  }

  return configs;
}

// Lazy adapter creation
async function createAdapter(config: ChannelConfig): Promise<any> {
  switch (config.type) {
    case "telegram": {
      const { TelegramAdapter } = await import("unified-channel/adapters/telegram");
      return new TelegramAdapter(config.config.token);
    }
    case "discord": {
      const { DiscordAdapter } = await import("unified-channel/adapters/discord");
      return new DiscordAdapter(config.config.token);
    }
    case "slack": {
      const { SlackAdapter } = await import("unified-channel/adapters/slack");
      return new SlackAdapter(config.config.botToken, config.config.appToken);
    }
    case "mattermost": {
      const { MattermostAdapter } = await import("unified-channel/adapters/mattermost");
      return new MattermostAdapter(config.config.url, config.config.token);
    }
    case "matrix": {
      const { MatrixAdapter } = await import("unified-channel/adapters/matrix");
      return new MatrixAdapter(config.config.homeserver, config.config.token);
    }
    default:
      throw new Error(`Unknown channel type: ${config.type}`);
  }
}

// Active adapters
const adapters = new Map<string, any>();

async function main() {
  const server = new McpServer({
    name: "unified-channel",
    version: "0.1.0",
  });

  // --- Tools ---

  server.tool(
    "send_message",
    "Send a message to a user/chat on any connected channel (Telegram, Discord, Slack, etc.)",
    {
      channel: z.string().describe("Channel to send on: telegram, discord, slack, mattermost, matrix, etc."),
      chat_id: z.string().describe("Chat/channel/room ID to send the message to"),
      text: z.string().describe("Message text to send"),
      reply_to_id: z.string().optional().describe("Optional message ID to reply to"),
    },
    async ({ channel, chat_id, text, reply_to_id }) => {
      const adapter = adapters.get(channel);
      if (!adapter) {
        return { content: [{ type: "text" as const, text: `Channel "${channel}" is not connected. Connected channels: ${[...adapters.keys()].join(", ") || "none"}` }] };
      }
      try {
        const msgId = await adapter.send({ chatId: chat_id, text, replyToId: reply_to_id });
        return { content: [{ type: "text" as const, text: `Message sent on ${channel} to ${chat_id}. Message ID: ${msgId ?? "unknown"}` }] };
      } catch (e: any) {
        return { content: [{ type: "text" as const, text: `Failed to send: ${e.message}` }], isError: true };
      }
    }
  );

  server.tool(
    "broadcast_message",
    "Send the same message to multiple channels at once",
    {
      text: z.string().describe("Message text to broadcast"),
      targets: z.record(z.string(), z.string()).describe("Map of channel name to chat ID, e.g. { telegram: '123', discord: '456' }"),
    },
    async ({ text, targets }) => {
      const results: string[] = [];
      for (const [channel, chatId] of Object.entries(targets)) {
        const adapter = adapters.get(channel);
        if (!adapter) {
          results.push(`${channel}: not connected`);
          continue;
        }
        try {
          await adapter.send({ chatId, text });
          results.push(`${channel}: sent`);
        } catch (e: any) {
          results.push(`${channel}: failed (${e.message})`);
        }
      }
      return { content: [{ type: "text" as const, text: results.join("\n") }] };
    }
  );

  server.tool(
    "get_channel_status",
    "Check the connection status of all configured channels",
    {},
    async () => {
      const statuses: string[] = [];
      for (const [name, adapter] of adapters) {
        try {
          const status = await adapter.getStatus();
          statuses.push(`${name}: ${status.connected ? "connected" : "disconnected"}${status.accountId ? ` (${status.accountId})` : ""}${status.lastActivity ? ` last active: ${status.lastActivity.toISOString()}` : ""}`);
        } catch (e: any) {
          statuses.push(`${name}: error (${e.message})`);
        }
      }
      if (statuses.length === 0) {
        return { content: [{ type: "text" as const, text: "No channels configured. Set UC_TELEGRAM_TOKEN, UC_DISCORD_TOKEN, etc. environment variables." }] };
      }
      return { content: [{ type: "text" as const, text: statuses.join("\n") }] };
    }
  );

  server.tool(
    "list_channels",
    "List all available channel types and which ones are currently connected",
    {},
    async () => {
      const allChannels = [
        "telegram", "discord", "slack", "whatsapp", "imessage", "matrix",
        "msteams", "line", "feishu", "mattermost", "googlechat", "nextcloud",
        "synology", "zalo", "nostr", "bluebubbles", "twitch", "irc",
      ];
      const lines = allChannels.map(ch => {
        const connected = adapters.has(ch);
        return `${connected ? "●" : "○"} ${ch}${connected ? " (connected)" : ""}`;
      });
      return { content: [{ type: "text" as const, text: `Channels:\n${lines.join("\n")}\n\nConnected: ${adapters.size}/${allChannels.length}` }] };
    }
  );

  server.tool(
    "get_recent_messages",
    "Get recent messages received across all connected channels",
    {
      limit: z.number().optional().describe("Max messages to return (default 20)"),
      channel: z.string().optional().describe("Filter by channel name"),
    },
    async ({ limit, channel }) => {
      let msgs = [...messageBuffer];
      if (channel) msgs = msgs.filter(m => m.channel === channel);
      msgs = msgs.slice(-(limit ?? 20));

      if (msgs.length === 0) {
        return { content: [{ type: "text" as const, text: "No recent messages." }] };
      }

      const formatted = msgs.map(m =>
        `[${m.timestamp}] ${m.channel}/${m.chatId} @${m.sender}: ${m.text}`
      ).join("\n");

      return { content: [{ type: "text" as const, text: formatted }] };
    }
  );

  // --- Resources ---

  server.resource(
    "channels-config",
    "unified-channel://config",
    async (uri) => ({
      contents: [{
        uri: uri.href,
        text: JSON.stringify({
          configured: [...adapters.keys()],
          environment_variables: {
            UC_TELEGRAM_TOKEN: "Telegram bot token",
            UC_DISCORD_TOKEN: "Discord bot token",
            UC_SLACK_BOT_TOKEN: "Slack bot token (xoxb-...)",
            UC_SLACK_APP_TOKEN: "Slack app token (xapp-...)",
            UC_MATTERMOST_URL: "Mattermost server URL",
            UC_MATTERMOST_TOKEN: "Mattermost access token",
            UC_MATRIX_HOMESERVER: "Matrix homeserver URL",
            UC_MATRIX_TOKEN: "Matrix access token",
          },
        }, null, 2),
        mimeType: "application/json",
      }],
    })
  );

  // --- Initialize channels ---

  const configs = parseChannelConfigs();
  for (const config of configs) {
    try {
      const adapter = await createAdapter(config);

      // Set up message buffering
      adapter.onMessage((msg: any) => {
        messageBuffer.push({
          id: msg.id,
          channel: msg.channel,
          sender: msg.sender?.username || msg.sender?.id || "unknown",
          text: msg.content?.text || "",
          timestamp: new Date().toISOString(),
          chatId: msg.chatId || "",
        });
        if (messageBuffer.length > MAX_BUFFER) {
          messageBuffer.splice(0, messageBuffer.length - MAX_BUFFER);
        }
      });

      await adapter.connect();
      adapters.set(config.type, adapter);
      console.error(`[unified-channel] Connected: ${config.type}`);
    } catch (e: any) {
      console.error(`[unified-channel] Failed to connect ${config.type}: ${e.message}`);
    }
  }

  if (adapters.size === 0) {
    console.error("[unified-channel] No channels configured. Set environment variables (UC_TELEGRAM_TOKEN, etc.) to enable channels.");
  }

  // Start MCP server
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`[unified-channel] MCP server running (${adapters.size} channels connected)`);
}

main().catch((e) => {
  console.error("[unified-channel] Fatal:", e);
  process.exit(1);
});
