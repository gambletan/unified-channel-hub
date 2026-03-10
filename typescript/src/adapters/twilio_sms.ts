/** Twilio SMS adapter — send/receive SMS via twilio SDK + webhook. */

import type { ChannelAdapter } from "../adapter.js";
import { ContentType, type ChannelStatus, type OutboundMessage, type UnifiedMessage } from "../types.js";

export class TwilioSMSAdapter implements ChannelAdapter {
  readonly channelId = "twilio_sms";
  private connected = false;
  private lastActivity?: Date;
  private handler?: (msg: UnifiedMessage) => void;
  private msgCounter = 0;
  private twilioClient: any;
  private server: any;

  constructor(
    private accountSid: string,
    private authToken: string,
    private phoneNumber: string,
    private options: { webhookPort?: number } = {}
  ) {
    this.options.webhookPort ??= 3201;
  }

  async connect(): Promise<void> {
    const twilio = await import("twilio");
    const http = await import("node:http");

    this.twilioClient = twilio.default(this.accountSid, this.authToken);

    this.server = http.createServer((req, res) => {
      if (req.method !== "POST" || !this.handler) { res.writeHead(404); res.end(); return; }
      let body = "";
      req.on("data", (c: Buffer) => { body += c.toString(); });
      req.on("end", () => {
        const params = new URLSearchParams(body);
        const from = params.get("From") ?? "unknown";
        const text = params.get("Body") ?? "";
        const sid = params.get("MessageSid") ?? "";
        const mediaUrl = params.get("MediaUrl0");
        this.msgCounter++;
        this.lastActivity = new Date();

        this.handler!({
          id: sid || String(this.msgCounter), channel: "twilio_sms",
          sender: { id: from, username: from },
          content: mediaUrl
            ? { type: ContentType.MEDIA, text, mediaUrl, mediaType: params.get("MediaContentType0") ?? undefined }
            : { type: ContentType.TEXT, text },
          timestamp: new Date(), chatId: from, raw: Object.fromEntries(params),
        });

        res.writeHead(200, { "Content-Type": "text/xml" });
        res.end("<Response></Response>");
      });
    });

    await new Promise<void>((resolve) => this.server.listen(this.options.webhookPort, resolve));
    this.connected = true;
  }

  async disconnect(): Promise<void> {
    await new Promise<void>((resolve) => this.server?.close(() => resolve()) ?? resolve());
    this.connected = false;
  }

  onMessage(handler: (msg: UnifiedMessage) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    const result = await this.twilioClient.messages.create({
      to: msg.chatId, from: this.phoneNumber, body: msg.text,
      ...(msg.mediaUrl ? { mediaUrl: [msg.mediaUrl] } : {}),
    });
    this.lastActivity = new Date();
    return result.sid;
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "twilio_sms", accountId: this.phoneNumber, lastActivity: this.lastActivity };
  }
}
