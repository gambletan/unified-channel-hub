import { describe, it, expect, vi, beforeEach } from "vitest";
import { ChannelManager } from "../src/manager.js";
import { ContentType } from "../src/types.js";
import type { ChannelAdapter } from "../src/adapter.js";
import type { UnifiedMessage, OutboundMessage, ChannelStatus } from "../src/types.js";
import type { Middleware, Handler, HandlerResult } from "../src/middleware.js";

function createMockAdapter(
  channelId: string,
  opts?: { statusDelay?: number; statusError?: boolean }
): ChannelAdapter & { triggerMessage: (msg: UnifiedMessage) => void; sentMessages: OutboundMessage[] } {
  let handler: ((msg: UnifiedMessage) => void) | undefined;
  const sentMessages: OutboundMessage[] = [];
  return {
    channelId,
    sentMessages,
    connect: vi.fn().mockResolvedValue(undefined),
    disconnect: vi.fn().mockResolvedValue(undefined),
    onMessage(h) { handler = h; },
    send: vi.fn().mockImplementation(async (msg: OutboundMessage) => {
      sentMessages.push(msg);
      return `sent-${sentMessages.length}`;
    }),
    getStatus: vi.fn().mockImplementation(async () => {
      if (opts?.statusDelay) await new Promise((r) => setTimeout(r, opts.statusDelay));
      if (opts?.statusError) throw new Error("status failed");
      return { connected: true, channel: channelId };
    }),
    triggerMessage(msg: UnifiedMessage) { handler?.(msg); },
  };
}

function makeMsg(channel: string, text: string, overrides: Partial<UnifiedMessage> = {}): UnifiedMessage {
  return {
    id: "1", channel, sender: { id: "user1" },
    content: { type: ContentType.TEXT, text },
    timestamp: new Date(), chatId: "c1",
    ...overrides,
  };
}

describe("Pipeline caching", () => {
  let manager: ChannelManager;
  let mockAdapter: ReturnType<typeof createMockAdapter>;

  beforeEach(() => {
    manager = new ChannelManager();
    mockAdapter = createMockAdapter("test");
    manager.addChannel(mockAdapter);
  });

  it("reuses cached pipeline across multiple messages", async () => {
    let callCount = 0;
    const mw: Middleware = {
      async process(msg, next) { callCount++; return next(msg); },
    };
    manager.addMiddleware(mw);
    manager.onMessage(async () => "ok");

    await mockAdapter.connect();
    mockAdapter.onMessage((msg) => (manager as any).handleMessage(mockAdapter, msg));

    // Trigger 3 messages
    mockAdapter.triggerMessage(makeMsg("test", "a"));
    mockAdapter.triggerMessage(makeMsg("test", "b"));
    mockAdapter.triggerMessage(makeMsg("test", "c"));
    await new Promise((r) => setTimeout(r, 100));

    expect(callCount).toBe(3);
    // Pipeline should be built once and cached
    expect((manager as any).cachedPipeline).toBeTruthy();
  });

  it("invalidates cache when middleware is added", async () => {
    const mw1: Middleware = { async process(msg, next) { return next(msg); } };
    manager.addMiddleware(mw1);
    manager.onMessage(async () => "ok");

    // Trigger to build cache
    await mockAdapter.connect();
    mockAdapter.onMessage((msg) => (manager as any).handleMessage(mockAdapter, msg));
    mockAdapter.triggerMessage(makeMsg("test", "a"));
    await new Promise((r) => setTimeout(r, 50));
    expect((manager as any).cachedPipeline).toBeTruthy();

    // Adding new middleware should invalidate
    const mw2: Middleware = { async process(msg, next) { return next(msg); } };
    manager.addMiddleware(mw2);
    expect((manager as any).cachedPipeline).toBeNull();
  });

  it("invalidates cache when fallback handler changes", async () => {
    manager.onMessage(async () => "first");
    // Trigger pipeline build
    await mockAdapter.connect();
    mockAdapter.onMessage((msg) => (manager as any).handleMessage(mockAdapter, msg));
    mockAdapter.triggerMessage(makeMsg("test", "a"));
    await new Promise((r) => setTimeout(r, 50));

    const cachedBefore = (manager as any).cachedPipeline;
    expect(cachedBefore).toBeTruthy();

    // Change handler
    manager.onMessage(async () => "second");
    expect((manager as any).cachedPipeline).toBeNull();
  });

  it("new pipeline reflects updated handler", async () => {
    manager.onMessage(async () => "first");
    await mockAdapter.connect();
    mockAdapter.onMessage((msg) => (manager as any).handleMessage(mockAdapter, msg));

    mockAdapter.triggerMessage(makeMsg("test", "a"));
    await new Promise((r) => setTimeout(r, 50));
    expect(mockAdapter.sentMessages[0].text).toBe("first");

    manager.onMessage(async () => "second");
    mockAdapter.triggerMessage(makeMsg("test", "b"));
    await new Promise((r) => setTimeout(r, 50));
    expect(mockAdapter.sentMessages[1].text).toBe("second");
  });
});

describe("Parallel getStatus", () => {
  it("fetches status from all adapters concurrently", async () => {
    const manager = new ChannelManager();
    const adapter1 = createMockAdapter("ch1", { statusDelay: 50 });
    const adapter2 = createMockAdapter("ch2", { statusDelay: 50 });
    const adapter3 = createMockAdapter("ch3", { statusDelay: 50 });
    manager.addChannel(adapter1).addChannel(adapter2).addChannel(adapter3);

    const start = Date.now();
    const statuses = await manager.getStatus();
    const elapsed = Date.now() - start;

    expect(Object.keys(statuses)).toHaveLength(3);
    expect(statuses.ch1.connected).toBe(true);
    expect(statuses.ch2.connected).toBe(true);
    // If sequential, would take ~150ms. Parallel should be ~50ms (+overhead)
    expect(elapsed).toBeLessThan(120);
  });

  it("handles errors from individual adapters without failing others", async () => {
    const manager = new ChannelManager();
    const good = createMockAdapter("good");
    const bad = createMockAdapter("bad", { statusError: true });
    manager.addChannel(good).addChannel(bad);

    const statuses = await manager.getStatus();
    expect(statuses.good.connected).toBe(true);
    expect(statuses.bad.connected).toBe(false);
    expect(statuses.bad.error).toContain("status failed");
  });
});

describe("Broadcast concurrency", () => {
  it("respects concurrency limit", async () => {
    const manager = new ChannelManager();
    manager.broadcastConcurrency = 2;

    let maxConcurrent = 0;
    let currentConcurrent = 0;

    // Create adapters that track concurrency
    for (let i = 0; i < 4; i++) {
      const adapter: ChannelAdapter = {
        channelId: `ch${i}`,
        connect: vi.fn().mockResolvedValue(undefined),
        disconnect: vi.fn().mockResolvedValue(undefined),
        onMessage() {},
        send: vi.fn().mockImplementation(async () => {
          currentConcurrent++;
          maxConcurrent = Math.max(maxConcurrent, currentConcurrent);
          await new Promise((r) => setTimeout(r, 30));
          currentConcurrent--;
          return "ok";
        }),
        getStatus: vi.fn().mockResolvedValue({ connected: true, channel: `ch${i}` }),
      };
      manager.addChannel(adapter);
    }

    await manager.broadcast("hello", { ch0: "c0", ch1: "c1", ch2: "c2", ch3: "c3" });

    // With concurrency=2, max concurrent should be 2, not 4
    expect(maxConcurrent).toBeLessThanOrEqual(2);
  });

  it("sends to all targets despite batching", async () => {
    const manager = new ChannelManager();
    manager.broadcastConcurrency = 2;

    const sentTo: string[] = [];
    for (let i = 0; i < 5; i++) {
      const adapter: ChannelAdapter = {
        channelId: `ch${i}`,
        connect: vi.fn().mockResolvedValue(undefined),
        disconnect: vi.fn().mockResolvedValue(undefined),
        onMessage() {},
        send: vi.fn().mockImplementation(async (msg: OutboundMessage) => {
          sentTo.push(msg.chatId);
          return "ok";
        }),
        getStatus: vi.fn().mockResolvedValue({ connected: true, channel: `ch${i}` }),
      };
      manager.addChannel(adapter);
    }

    await manager.broadcast("hi", { ch0: "a", ch1: "b", ch2: "c", ch3: "d", ch4: "e" });
    expect(sentTo.sort()).toEqual(["a", "b", "c", "d", "e"]);
  });
});

describe("Event-driven shutdown", () => {
  it("resolves run() when shutdown() is called", async () => {
    const manager = new ChannelManager();
    const adapter = createMockAdapter("test");
    manager.addChannel(adapter);

    let runResolved = false;
    const runPromise = manager.run().then(() => { runResolved = true; });

    // Give run() time to start
    await new Promise((r) => setTimeout(r, 50));
    expect(runResolved).toBe(false);

    await manager.shutdown();

    // run() should resolve quickly after shutdown
    await Promise.race([
      runPromise,
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 500)),
    ]);
    expect(runResolved).toBe(true);
  });

  it("handles rapid shutdown after run", async () => {
    const manager = new ChannelManager();
    const adapter = createMockAdapter("test");
    manager.addChannel(adapter);

    const runPromise = manager.run();

    // Immediate shutdown after run starts
    await new Promise((r) => setTimeout(r, 10));
    await manager.shutdown();

    // run() should resolve without hanging
    await Promise.race([
      runPromise,
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 500)),
    ]);
    expect(adapter.disconnect).toHaveBeenCalled();
  });
});

describe("Broadcast returns PromiseSettledResult", () => {
  it("returns settled results with failure details", async () => {
    const manager = new ChannelManager();
    manager.broadcastConcurrency = 10;

    // Good adapter
    const good: ChannelAdapter = {
      channelId: "good",
      connect: vi.fn().mockResolvedValue(undefined),
      disconnect: vi.fn().mockResolvedValue(undefined),
      onMessage() {},
      send: vi.fn().mockResolvedValue("ok"),
      getStatus: vi.fn().mockResolvedValue({ connected: true, channel: "good" }),
    };
    // Bad adapter
    const bad: ChannelAdapter = {
      channelId: "bad",
      connect: vi.fn().mockResolvedValue(undefined),
      disconnect: vi.fn().mockResolvedValue(undefined),
      onMessage() {},
      send: vi.fn().mockRejectedValue(new Error("send failed")),
      getStatus: vi.fn().mockResolvedValue({ connected: true, channel: "bad" }),
    };
    manager.addChannel(good).addChannel(bad);

    const results = await manager.broadcast("hello", { good: "c1", bad: "c2" });
    expect(results).toHaveLength(2);

    const fulfilled = results.filter((r) => r.status === "fulfilled");
    const rejected = results.filter((r) => r.status === "rejected");
    expect(fulfilled).toHaveLength(1);
    expect(rejected).toHaveLength(1);
    expect((rejected[0] as PromiseRejectedResult).reason.message).toBe("send failed");
  });
});

describe("Middleware pipeline correctness", () => {
  it("executes middleware in order with cached pipeline", async () => {
    const manager = new ChannelManager();
    const adapter = createMockAdapter("test");
    manager.addChannel(adapter);

    const order: number[] = [];
    for (let i = 0; i < 5; i++) {
      const idx = i;
      manager.addMiddleware({
        async process(msg, next) { order.push(idx); return next(msg); },
      });
    }
    manager.onMessage(async () => "done");

    await adapter.connect();
    adapter.onMessage((msg) => (manager as any).handleMessage(adapter, msg));

    // First message — builds pipeline
    adapter.triggerMessage(makeMsg("test", "a"));
    await new Promise((r) => setTimeout(r, 50));
    expect(order).toEqual([0, 1, 2, 3, 4]);

    // Second message — reuses cached pipeline
    order.length = 0;
    adapter.triggerMessage(makeMsg("test", "b"));
    await new Promise((r) => setTimeout(r, 50));
    expect(order).toEqual([0, 1, 2, 3, 4]);
  });

  it("middleware can short-circuit the chain", async () => {
    const manager = new ChannelManager();
    const adapter = createMockAdapter("test");
    manager.addChannel(adapter);

    manager.addMiddleware({
      async process(msg, _next) { return "blocked"; }, // never calls next
    });

    let handlerCalled = false;
    manager.onMessage(async () => { handlerCalled = true; return "ok"; });

    await adapter.connect();
    adapter.onMessage((msg) => (manager as any).handleMessage(adapter, msg));
    adapter.triggerMessage(makeMsg("test", "a"));
    await new Promise((r) => setTimeout(r, 50));

    expect(handlerCalled).toBe(false);
    expect(adapter.sentMessages[0].text).toBe("blocked");
  });

  it("handles null reply (no response sent)", async () => {
    const manager = new ChannelManager();
    const adapter = createMockAdapter("test");
    manager.addChannel(adapter);
    manager.onMessage(async () => null);

    await adapter.connect();
    adapter.onMessage((msg) => (manager as any).handleMessage(adapter, msg));
    adapter.triggerMessage(makeMsg("test", "a"));
    await new Promise((r) => setTimeout(r, 50));

    expect(adapter.sentMessages).toHaveLength(0);
  });
});
