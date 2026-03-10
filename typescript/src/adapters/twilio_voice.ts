/** Twilio Voice adapter — outbound calls + inbound webhook via twilio SDK. */

import type { ChannelAdapter } from "../adapter.js";
import { ContentType, type ChannelStatus, type OutboundMessage, type UnifiedMessage } from "../types.js";

export class TwilioVoiceAdapter implements ChannelAdapter {
  readonly channelId = "twilio_voice";
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
    this.options.webhookPort ??= 3200;
  }

  async connect(): Promise<void> {
    const twilio = await import("twilio");
    const http = await import("node:http");
    const { URL } = await import("node:url");

    this.twilioClient = twilio.default(this.accountSid, this.authToken);

    this.server = http.createServer((req, res) => {
      if (req.method !== "POST" || !this.handler) { res.writeHead(404); res.end(); return; }
      let body = "";
      req.on("data", (c: Buffer) => { body += c.toString(); });
      req.on("end", () => {
        const params = new URLSearchParams(body);
        const from = params.get("From") ?? "unknown";
        const callSid = params.get("CallSid") ?? "";
        const speechResult = params.get("SpeechResult") ?? "";
        this.msgCounter++;
        this.lastActivity = new Date();

        const url = new URL(req.url ?? "/", `http://localhost:${this.options.webhookPort}`);
        if (url.pathname === "/voice/status") {
          const status = params.get("CallStatus") ?? "";
          this.handler!({
            id: String(this.msgCounter), channel: "twilio_voice",
            sender: { id: from }, content: { type: ContentType.TEXT, text: `[call_status: ${status}]` },
            timestamp: new Date(), chatId: from, raw: Object.fromEntries(params),
          });
        } else {
          this.handler!({
            id: callSid || String(this.msgCounter), channel: "twilio_voice",
            sender: { id: from }, content: { type: ContentType.TEXT, text: speechResult || "[incoming_call]" },
            timestamp: new Date(), chatId: from, raw: Object.fromEntries(params),
          });
          res.writeHead(200, { "Content-Type": "text/xml" });
          res.end("<Response><Gather input=\"speech\" action=\"/voice\"><Say>Please speak after the beep.</Say></Gather></Response>");
          return;
        }
        res.writeHead(200); res.end();
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
    const twiml = msg.metadata?.twiml as string ?? `<Response><Say>${msg.text}</Say></Response>`;
    const call = await this.twilioClient.calls.create({
      to: msg.chatId, from: this.phoneNumber, twiml,
      statusCallback: msg.metadata?.statusCallback as string | undefined,
    });
    this.lastActivity = new Date();
    return call.sid;
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "twilio_voice", accountId: this.phoneNumber, lastActivity: this.lastActivity };
  }
}
