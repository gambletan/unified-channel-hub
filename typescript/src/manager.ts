/** ChannelManager — ties adapters + middleware together. */

import type { ChannelAdapter } from "./adapter.js";
import type { Handler, HandlerResult, Middleware } from "./middleware.js";
import type { ChannelStatus, OutboundMessage, UnifiedMessage } from "./types.js";

export class ChannelManager {
  private channels = new Map<string, ChannelAdapter>();
  private middlewares: Middleware[] = [];
  private fallbackHandler: Handler | null = null;
  private running = false;
  private cachedPipeline: Handler | null = null;
  private shutdownResolve: (() => void) | null = null;
  /** Max concurrent sends in broadcast(). */
  broadcastConcurrency = 10;

  addChannel(adapter: ChannelAdapter): this {
    this.channels.set(adapter.channelId, adapter);
    return this;
  }

  addMiddleware(mw: Middleware): this {
    this.middlewares.push(mw);
    this.cachedPipeline = null; // invalidate cached chain
    return this;
  }

  onMessage(handler: Handler): this {
    this.fallbackHandler = handler;
    this.cachedPipeline = null; // invalidate cached chain
    return this;
  }

  async send(
    channel: string,
    chatId: string,
    text: string,
    options?: { replyToId?: string; parseMode?: string }
  ): Promise<string | undefined> {
    const adapter = this.channels.get(channel);
    if (!adapter) throw new Error(`Channel not registered: ${channel}`);
    return adapter.send({
      chatId,
      text,
      replyToId: options?.replyToId,
      parseMode: options?.parseMode,
    });
  }

  async broadcast(
    text: string,
    chatIds: Record<string, string>
  ): Promise<PromiseSettledResult<string | undefined>[]> {
    const entries = Object.entries(chatIds);
    const allResults: PromiseSettledResult<string | undefined>[] = [];
    // Send in batches to avoid overwhelming connections / rate limits
    for (let i = 0; i < entries.length; i += this.broadcastConcurrency) {
      const batch = entries.slice(i, i + this.broadcastConcurrency);
      const results = await Promise.allSettled(
        batch.map(([channel, chatId]) => this.send(channel, chatId, text))
      );
      allResults.push(...results);
    }
    return allResults;
  }

  async getStatus(): Promise<Record<string, ChannelStatus>> {
    // Parallel status fetch across all adapters
    const ids = [...this.channels.keys()];
    const results = await Promise.all(
      ids.map(async (id) => {
        try {
          return await this.channels.get(id)!.getStatus();
        } catch (e) {
          return { connected: false, channel: id, error: String(e) } as ChannelStatus;
        }
      })
    );
    const statuses: Record<string, ChannelStatus> = {};
    for (let i = 0; i < ids.length; i++) statuses[ids[i]] = results[i];
    return statuses;
  }

  async run(): Promise<void> {
    if (this.channels.size === 0) {
      throw new Error("No channels registered");
    }

    this.running = true;

    for (const adapter of this.channels.values()) {
      await adapter.connect();
      adapter.onMessage((msg) => this.handleMessage(adapter, msg));
    }

    console.log(
      `unified-channel started: channels=[${[...this.channels.keys()].join(", ")}]`
    );

    // Keep alive — event-driven instead of polling
    await new Promise<void>((resolve) => {
      this.shutdownResolve = resolve;
      if (!this.running) resolve();
    });
  }

  async shutdown(): Promise<void> {
    this.running = false;
    for (const adapter of this.channels.values()) {
      try {
        await adapter.disconnect();
      } catch (e) {
        console.error(`Error disconnecting ${adapter.channelId}:`, e);
      }
    }
    this.shutdownResolve?.();
    this.shutdownResolve = null;
    console.log("unified-channel shut down");
  }

  private async handleMessage(
    adapter: ChannelAdapter,
    msg: UnifiedMessage
  ): Promise<void> {
    try {
      const reply = await this.runPipeline(msg);
      if (reply && msg.chatId) {
        const out = this.toOutbound(reply, msg);
        await adapter.send(out);
      }
    } catch (e) {
      console.error(`Error processing message ${msg.id} on ${msg.channel}:`, e);
    }
  }

  private buildPipeline(): Handler {
    let handler: Handler = async (m) => {
      if (this.fallbackHandler) return this.fallbackHandler(m);
      return null;
    };

    for (let i = this.middlewares.length - 1; i >= 0; i--) {
      const mw = this.middlewares[i];
      const next = handler;
      handler = async (m) => mw.process(m, next);
    }

    return handler;
  }

  private async runPipeline(msg: UnifiedMessage): Promise<HandlerResult> {
    if (!this.cachedPipeline) {
      this.cachedPipeline = this.buildPipeline();
    }
    return this.cachedPipeline(msg);
  }

  private toOutbound(
    reply: string | OutboundMessage,
    orig: UnifiedMessage
  ): OutboundMessage {
    if (typeof reply === "string") {
      return { chatId: orig.chatId || "", text: reply, replyToId: orig.id };
    }
    if (!reply.chatId) reply.chatId = orig.chatId || "";
    return reply;
  }
}
