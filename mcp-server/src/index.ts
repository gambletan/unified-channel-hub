#!/usr/bin/env node

/**
 * unified-channel MCP Server
 *
 * Exposes messaging channels as MCP tools for AI agents.
 * Self-contained — no dependency on the unified-channel npm package.
 *
 * Usage:
 *   UC_TELEGRAM_TOKEN=... npx @unified-channel/mcp-server
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

// ---- Minimal adapter types (self-contained) ----

interface OutboundMessage {
  chatId: string;
  text: string;
  replyToId?: string;
}

interface ChannelStatus {
  connected: boolean;
  channel: string;
  accountId?: string;
  lastActivity?: Date;
}

interface ChannelAdapter {
  readonly channelId: string;
  connect(): Promise<void>;
  disconnect(): Promise<void>;
  onMessage(handler: (msg: any) => void): void;
  send(msg: OutboundMessage): Promise<string | undefined>;
  getStatus(): Promise<ChannelStatus>;
}

// ---- Telegram Adapter (grammy) ----

class TelegramAdapter implements ChannelAdapter {
  readonly channelId = "telegram";
  private connected = false;
  private lastActivity?: Date;
  private bot: any;
  private handler?: (msg: any) => void;
  private botUsername?: string;

  constructor(private token: string) {}

  async connect(): Promise<void> {
    const { Bot } = await import("grammy");
    this.bot = new Bot(this.token);

    this.bot.on("message:text", (ctx: any) => {
      if (!this.handler) return;
      const text: string = ctx.message.text;
      const isCmd = text.startsWith("/");
      const parts = isCmd ? text.slice(1).split(/\s+/) : [];
      this.lastActivity = new Date();

      this.handler({
        id: String(ctx.message.message_id), channel: "telegram",
        sender: { id: String(ctx.from.id), username: ctx.from.username, displayName: [ctx.from.first_name, ctx.from.last_name].filter(Boolean).join(" ") },
        content: { type: isCmd ? "command" : "text", text, command: isCmd ? parts[0]?.split("@")[0] : undefined, args: isCmd ? parts.slice(1) : undefined },
        timestamp: new Date(ctx.message.date * 1000), chatId: String(ctx.chat.id), raw: ctx,
      });
    });

    const me = await this.bot.api.getMe();
    this.botUsername = me.username;
    this.bot.start({ drop_pending_updates: true });
    this.connected = true;
  }

  async disconnect(): Promise<void> { await this.bot?.stop(); this.connected = false; }
  onMessage(handler: (msg: any) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    const opts: any = {};
    if (msg.replyToId) opts.reply_to_message_id = Number(msg.replyToId);
    const sent = await this.bot.api.sendMessage(Number(msg.chatId), msg.text, opts);
    this.lastActivity = new Date();
    return String(sent.message_id);
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "telegram", accountId: this.botUsername, lastActivity: this.lastActivity };
  }
}

// ---- Discord Adapter (discord.js) ----

class DiscordAdapter implements ChannelAdapter {
  readonly channelId = "discord";
  private connected = false;
  private lastActivity?: Date;
  private client: any;
  private handler?: (msg: any) => void;
  private botUsername?: string;

  constructor(private token: string) {}

  async connect(): Promise<void> {
    const { Client, GatewayIntentBits, Events } = await import("discord.js");
    this.client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent] });

    this.client.on(Events.MessageCreate, (message: any) => {
      if (message.author.bot || !this.handler) return;
      const text: string = message.content;
      const isCmd = text.startsWith("!");
      const parts = isCmd ? text.slice(1).split(/\s+/) : [];
      this.lastActivity = new Date();

      this.handler({
        id: message.id, channel: "discord",
        sender: { id: message.author.id, username: message.author.username, displayName: message.author.displayName },
        content: { type: isCmd ? "command" : "text", text, command: isCmd ? parts[0] : undefined, args: isCmd ? parts.slice(1) : undefined },
        timestamp: message.createdAt, chatId: message.channelId, threadId: message.thread?.id, raw: message,
      });
    });

    await this.client.login(this.token);
    this.botUsername = this.client.user?.username;
    this.connected = true;
  }

  async disconnect(): Promise<void> { await this.client?.destroy(); this.connected = false; }
  onMessage(handler: (msg: any) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    const channel = await this.client.channels.fetch(msg.chatId);
    if (!channel?.isTextBased()) throw new Error(`Channel ${msg.chatId} is not text-based`);
    const opts: any = { content: msg.text };
    if (msg.replyToId) opts.reply = { messageReference: msg.replyToId };
    const sent = await channel.send(opts);
    this.lastActivity = new Date();
    return sent.id;
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "discord", accountId: this.botUsername, lastActivity: this.lastActivity };
  }
}

// ---- Slack Adapter (@slack/bolt) ----

class SlackAdapter implements ChannelAdapter {
  readonly channelId = "slack";
  private connected = false;
  private lastActivity?: Date;
  private app: any;
  private handler?: (msg: any) => void;
  private botUserId?: string;

  constructor(private botToken: string, private appToken: string) {}

  async connect(): Promise<void> {
    const { App } = await import("@slack/bolt");
    this.app = new App({ token: this.botToken, appToken: this.appToken, socketMode: true });

    this.app.message(async ({ message, say }: any) => {
      if (message.subtype || !this.handler) return;
      const text: string = message.text || "";
      const isCmd = text.startsWith("/");
      const parts = isCmd ? text.slice(1).split(/\s+/) : [];
      this.lastActivity = new Date();

      this.handler({
        id: message.ts, channel: "slack",
        sender: { id: message.user },
        content: { type: isCmd ? "command" : "text", text, command: isCmd ? parts[0] : undefined, args: isCmd ? parts.slice(1) : undefined },
        timestamp: new Date(parseFloat(message.ts) * 1000), chatId: message.channel, threadId: message.thread_ts, raw: message,
      });
    });

    await this.app.start();
    const auth = await this.app.client.auth.test({ token: this.botToken });
    this.botUserId = auth.user_id;
    this.connected = true;
  }

  async disconnect(): Promise<void> { await this.app?.stop(); this.connected = false; }
  onMessage(handler: (msg: any) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    const result = await this.app.client.chat.postMessage({ token: this.botToken, channel: msg.chatId, text: msg.text, thread_ts: msg.replyToId });
    this.lastActivity = new Date();
    return result.ts;
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "slack", accountId: this.botUserId, lastActivity: this.lastActivity };
  }
}

// ---- Mattermost Adapter (WebSocket + REST) ----

class MattermostAdapter implements ChannelAdapter {
  readonly channelId = "mattermost";
  private connected = false;
  private lastActivity?: Date;
  private handler?: (msg: any) => void;
  private ws: any;
  private botUserId?: string;

  constructor(private url: string, private token: string) {}

  async connect(): Promise<void> {
    const baseUrl = this.url.replace(/\/$/, "");
    const meResp = await fetch(`${baseUrl}/api/v4/users/me`, { headers: { Authorization: `Bearer ${this.token}` } });
    const me = await meResp.json() as any;
    this.botUserId = me.id;

    const { default: WebSocket } = await import("ws");
    const wsUrl = baseUrl.replace(/^http/, "ws") + "/api/v4/websocket";
    this.ws = new WebSocket(wsUrl);

    this.ws.on("open", () => {
      this.ws.send(JSON.stringify({ seq: 1, action: "authentication_challenge", data: { token: this.token } }));
      this.connected = true;
    });

    this.ws.on("message", (raw: string) => {
      const event = JSON.parse(raw);
      if (event.event !== "posted" || !this.handler) return;
      const post = JSON.parse(event.data?.post || "{}");
      if (post.user_id === this.botUserId) return;
      const text: string = post.message || "";
      this.lastActivity = new Date();

      this.handler({
        id: post.id, channel: "mattermost",
        sender: { id: post.user_id },
        content: { type: "text", text },
        timestamp: new Date(post.create_at), chatId: post.channel_id, threadId: post.root_id || undefined, raw: post,
      });
    });

    await new Promise<void>((resolve) => this.ws.once("open", resolve));
  }

  async disconnect(): Promise<void> { this.ws?.close(); this.connected = false; }
  onMessage(handler: (msg: any) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    const resp = await fetch(`${this.url.replace(/\/$/, "")}/api/v4/posts`, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id: msg.chatId, message: msg.text, root_id: msg.replyToId }),
    });
    const data = await resp.json() as any;
    this.lastActivity = new Date();
    return data.id;
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "mattermost", accountId: this.botUserId, lastActivity: this.lastActivity };
  }
}

// ---- IRC Adapter (net.Socket, zero deps) ----

class IRCAdapter implements ChannelAdapter {
  readonly channelId = "irc";
  private connected = false;
  private lastActivity?: Date;
  private handler?: (msg: any) => void;
  private socket: any;
  private msgCounter = 0;

  constructor(private server: string, private nickname: string, private channels: string[], private port = 6667) {}

  async connect(): Promise<void> {
    const net = await import("net");
    this.socket = net.createConnection(this.port, this.server);
    let buffer = "";

    this.socket.on("data", (data: Buffer) => {
      buffer += data.toString();
      const lines = buffer.split("\r\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("PING")) {
          this.socket.write(`PONG ${line.slice(5)}\r\n`);
          continue;
        }
        // :nick!user@host PRIVMSG #channel :message
        const match = line.match(/^:(\S+?)!\S+ PRIVMSG (\S+) :(.*)$/);
        if (match && this.handler) {
          const [, nick, target, text] = match;
          if (nick === this.nickname) continue;
          this.msgCounter++;
          this.lastActivity = new Date();
          this.handler({
            id: String(this.msgCounter), channel: "irc",
            sender: { id: nick, username: nick },
            content: { type: "text", text },
            timestamp: new Date(), chatId: target, raw: line,
          });
        }
      }
    });

    this.socket.on("connect", () => {
      this.socket.write(`NICK ${this.nickname}\r\n`);
      this.socket.write(`USER ${this.nickname} 0 * :unified-channel\r\n`);
      for (const ch of this.channels) this.socket.write(`JOIN ${ch}\r\n`);
      this.connected = true;
    });

    await new Promise<void>((resolve) => this.socket.once("connect", resolve));
  }

  async disconnect(): Promise<void> { this.socket?.write("QUIT :bye\r\n"); this.socket?.destroy(); this.connected = false; }
  onMessage(handler: (msg: any) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    for (const line of msg.text.split("\n")) {
      if (line.trim()) this.socket.write(`PRIVMSG ${msg.chatId} :${line}\r\n`);
    }
    this.lastActivity = new Date();
    return undefined;
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "irc", accountId: `${this.nickname}@${this.server}`, lastActivity: this.lastActivity };
  }
}

// ---- MCP Server ----

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
const adapters = new Map<string, ChannelAdapter>();

function parseAndCreateAdapters(): { type: string; create: () => ChannelAdapter }[] {
  const result: { type: string; create: () => ChannelAdapter }[] = [];

  if (process.env.UC_TELEGRAM_TOKEN) {
    result.push({ type: "telegram", create: () => new TelegramAdapter(process.env.UC_TELEGRAM_TOKEN!) });
  }
  if (process.env.UC_DISCORD_TOKEN) {
    result.push({ type: "discord", create: () => new DiscordAdapter(process.env.UC_DISCORD_TOKEN!) });
  }
  if (process.env.UC_SLACK_BOT_TOKEN && process.env.UC_SLACK_APP_TOKEN) {
    result.push({ type: "slack", create: () => new SlackAdapter(process.env.UC_SLACK_BOT_TOKEN!, process.env.UC_SLACK_APP_TOKEN!) });
  }
  if (process.env.UC_MATTERMOST_URL && process.env.UC_MATTERMOST_TOKEN) {
    result.push({ type: "mattermost", create: () => new MattermostAdapter(process.env.UC_MATTERMOST_URL!, process.env.UC_MATTERMOST_TOKEN!) });
  }
  if (process.env.UC_IRC_SERVER && process.env.UC_IRC_NICK && process.env.UC_IRC_CHANNELS) {
    result.push({ type: "irc", create: () => new IRCAdapter(process.env.UC_IRC_SERVER!, process.env.UC_IRC_NICK!, process.env.UC_IRC_CHANNELS!.split(","), Number(process.env.UC_IRC_PORT) || 6667) });
  }

  return result;
}

async function main() {
  const server = new McpServer({
    name: "unified-channel",
    version: "0.1.0",
  });

  // ---- Tools ----

  server.tool(
    "send_message",
    "Send a message to a user/chat on any connected channel (Telegram, Discord, Slack, etc.)",
    {
      channel: z.string().describe("Channel to send on: telegram, discord, slack, mattermost, irc"),
      chat_id: z.string().describe("Chat/channel/room ID to send the message to"),
      text: z.string().describe("Message text to send"),
      reply_to_id: z.string().optional().describe("Optional message ID to reply to"),
    },
    async ({ channel, chat_id, text, reply_to_id }) => {
      const adapter = adapters.get(channel);
      if (!adapter) {
        return { content: [{ type: "text" as const, text: `Channel "${channel}" is not connected. Connected: ${[...adapters.keys()].join(", ") || "none"}` }] };
      }
      try {
        const msgId = await adapter.send({ chatId: chat_id, text, replyToId: reply_to_id });
        return { content: [{ type: "text" as const, text: `Sent on ${channel} to ${chat_id}. ID: ${msgId ?? "unknown"}` }] };
      } catch (e: any) {
        return { content: [{ type: "text" as const, text: `Failed: ${e.message}` }], isError: true };
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
      for (const [ch, chatId] of Object.entries(targets)) {
        const adapter = adapters.get(ch);
        if (!adapter) { results.push(`${ch}: not connected`); continue; }
        try { await adapter.send({ chatId, text }); results.push(`${ch}: sent`); }
        catch (e: any) { results.push(`${ch}: failed (${e.message})`); }
      }
      return { content: [{ type: "text" as const, text: results.join("\n") }] };
    }
  );

  server.tool(
    "get_channel_status",
    "Check the connection status of all configured channels",
    {},
    async () => {
      if (adapters.size === 0) {
        return { content: [{ type: "text" as const, text: "No channels configured. Set UC_TELEGRAM_TOKEN, UC_DISCORD_TOKEN, etc." }] };
      }
      const lines: string[] = [];
      for (const [name, adapter] of adapters) {
        try {
          const s = await adapter.getStatus();
          lines.push(`${s.connected ? "●" : "○"} ${name}${s.accountId ? ` (${s.accountId})` : ""}${s.lastActivity ? ` — last: ${s.lastActivity.toISOString()}` : ""}`);
        } catch (e: any) { lines.push(`✗ ${name}: ${e.message}`); }
      }
      return { content: [{ type: "text" as const, text: lines.join("\n") }] };
    }
  );

  server.tool(
    "list_channels",
    "List all supported channel types and which ones are currently connected",
    {},
    async () => {
      const all = ["telegram", "discord", "slack", "whatsapp", "imessage", "matrix", "msteams", "line", "feishu", "mattermost", "googlechat", "nextcloud", "synology", "zalo", "nostr", "bluebubbles", "twitch", "irc"];
      const lines = all.map(ch => `${adapters.has(ch) ? "●" : "○"} ${ch}${adapters.has(ch) ? " (connected)" : ""}`);
      return { content: [{ type: "text" as const, text: `Channels:\n${lines.join("\n")}\n\nConnected: ${adapters.size}/${all.length}` }] };
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
      if (msgs.length === 0) return { content: [{ type: "text" as const, text: "No recent messages." }] };
      const formatted = msgs.map(m => `[${m.timestamp}] ${m.channel}/${m.chatId} @${m.sender}: ${m.text}`).join("\n");
      return { content: [{ type: "text" as const, text: formatted }] };
    }
  );

  // ---- Resource ----

  server.resource(
    "channels-config",
    "unified-channel://config",
    async (uri) => ({
      contents: [{
        uri: uri.href,
        text: JSON.stringify({
          connected: [...adapters.keys()],
          env_vars: {
            UC_TELEGRAM_TOKEN: "Telegram bot token",
            UC_DISCORD_TOKEN: "Discord bot token",
            UC_SLACK_BOT_TOKEN: "Slack bot token (xoxb-...)",
            UC_SLACK_APP_TOKEN: "Slack app token (xapp-...)",
            UC_MATTERMOST_URL: "Mattermost server URL",
            UC_MATTERMOST_TOKEN: "Mattermost access token",
            UC_IRC_SERVER: "IRC server hostname",
            UC_IRC_NICK: "IRC nickname",
            UC_IRC_CHANNELS: "Comma-separated IRC channels",
          },
        }, null, 2),
        mimeType: "application/json",
      }],
    })
  );

  // ---- Connect channels ----

  const configs = parseAndCreateAdapters();
  for (const { type, create } of configs) {
    try {
      const adapter = create();
      adapter.onMessage((msg: any) => {
        messageBuffer.push({
          id: msg.id, channel: msg.channel,
          sender: msg.sender?.username || msg.sender?.id || "unknown",
          text: msg.content?.text || "", timestamp: new Date().toISOString(),
          chatId: msg.chatId || "",
        });
        if (messageBuffer.length > MAX_BUFFER) messageBuffer.splice(0, messageBuffer.length - MAX_BUFFER);
      });
      await adapter.connect();
      adapters.set(type, adapter);
      console.error(`[unified-channel] ● ${type} connected`);
    } catch (e: any) {
      console.error(`[unified-channel] ✗ ${type}: ${e.message}`);
    }
  }

  if (adapters.size === 0) {
    console.error("[unified-channel] No channels configured. Set UC_TELEGRAM_TOKEN, UC_DISCORD_TOKEN, etc.");
  }

  // ---- Start ----

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`[unified-channel] MCP server running (${adapters.size} channels)`);
}

main().catch((e) => {
  console.error("[unified-channel] Fatal:", e);
  process.exit(1);
});
