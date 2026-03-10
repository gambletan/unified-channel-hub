/** Home Assistant adapter — WebSocket API for state changes + service calls. */

import type { ChannelAdapter } from "../adapter.js";
import { ContentType, type ChannelStatus, type OutboundMessage, type UnifiedMessage } from "../types.js";

export class HomeAssistantAdapter implements ChannelAdapter {
  readonly channelId = "homeassistant";
  private connected = false;
  private lastActivity?: Date;
  private handler?: (msg: UnifiedMessage) => void;
  private msgCounter = 0;
  private ws: any;
  private cmdId = 0;
  private entityFilters?: string[];

  constructor(
    private url: string,
    private accessToken: string,
    private options: { entityFilters?: string[] } = {}
  ) {
    this.entityFilters = options.entityFilters;
  }

  async connect(): Promise<void> {
    const { default: WebSocket } = await import("ws");
    const wsUrl = this.url.replace(/^http/, "ws") + "/api/websocket";

    await new Promise<void>((resolve, reject) => {
      this.ws = new WebSocket(wsUrl);

      this.ws.on("message", (data: Buffer) => {
        const msg = JSON.parse(data.toString());

        if (msg.type === "auth_required") {
          this.ws.send(JSON.stringify({ type: "auth", access_token: this.accessToken }));
        } else if (msg.type === "auth_ok") {
          this.cmdId++;
          this.ws.send(JSON.stringify({ id: this.cmdId, type: "subscribe_events", event_type: "state_changed" }));
          this.connected = true;
          resolve();
        } else if (msg.type === "auth_invalid") {
          reject(new Error("Home Assistant auth failed"));
        } else if (msg.type === "event" && msg.event?.event_type === "state_changed") {
          this.handleStateChange(msg.event.data);
        }
      });

      this.ws.on("error", (err: Error) => { if (!this.connected) reject(err); });
      this.ws.on("close", () => { this.connected = false; });
    });
  }

  async disconnect(): Promise<void> {
    this.ws?.close();
    this.connected = false;
  }

  onMessage(handler: (msg: UnifiedMessage) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    const [domain, service] = msg.text.split(".", 2);
    if (!domain || !service) throw new Error("msg.text must be 'domain.service' (e.g. 'light.turn_on')");

    this.cmdId++;
    const payload: any = {
      id: this.cmdId, type: "call_service", domain, service,
      target: { entity_id: msg.chatId },
    };
    if (msg.metadata?.serviceData) payload.service_data = msg.metadata.serviceData;

    this.ws.send(JSON.stringify(payload));
    this.lastActivity = new Date();
    return String(this.cmdId);
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "homeassistant", accountId: this.url, lastActivity: this.lastActivity };
  }

  private handleStateChange(data: any): void {
    if (!this.handler) return;
    const entityId: string = data.entity_id ?? "";
    if (this.entityFilters?.length && !this.entityFilters.some((f) => entityId.startsWith(f))) return;

    const newState = data.new_state;
    if (!newState) return;

    this.msgCounter++;
    this.lastActivity = new Date();
    this.handler({
      id: String(this.msgCounter), channel: "homeassistant",
      sender: { id: entityId, displayName: newState.attributes?.friendly_name },
      content: { type: ContentType.TEXT, text: `${entityId}: ${newState.state}` },
      timestamp: new Date(newState.last_changed ?? Date.now()),
      chatId: entityId, raw: data,
      metadata: { oldState: data.old_state?.state, newState: newState.state, attributes: newState.attributes },
    });
  }
}
