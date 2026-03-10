/** Email adapter — IMAP polling (receive) + SMTP via nodemailer (send). */

import type { ChannelAdapter } from "../adapter.js";
import { ContentType, type ChannelStatus, type OutboundMessage, type UnifiedMessage } from "../types.js";

const PRESETS: Record<string, { imapHost: string; smtpHost: string; smtpPort: number }> = {
  gmail: { imapHost: "imap.gmail.com", smtpHost: "smtp.gmail.com", smtpPort: 465 },
  outlook: { imapHost: "outlook.office365.com", smtpHost: "smtp.office365.com", smtpPort: 587 },
};

export class EmailAdapter implements ChannelAdapter {
  readonly channelId = "email";
  private connected = false;
  private lastActivity?: Date;
  private handler?: (msg: UnifiedMessage) => void;
  private msgCounter = 0;
  private pollTimer?: ReturnType<typeof setInterval>;
  private transporter: any;
  private imapClient: any;
  private imapHost: string;
  private smtpHost: string;
  private smtpPort: number;
  private pollInterval: number;

  constructor(
    private emailAddress: string,
    private password: string,
    private options: { imapHost?: string; smtpHost?: string; smtpPort?: number; preset?: string; pollInterval?: number } = {}
  ) {
    const preset = options.preset ? PRESETS[options.preset] : undefined;
    this.imapHost = options.imapHost ?? preset?.imapHost ?? "localhost";
    this.smtpHost = options.smtpHost ?? preset?.smtpHost ?? "localhost";
    this.smtpPort = options.smtpPort ?? preset?.smtpPort ?? 465;
    this.pollInterval = options.pollInterval ?? 30_000;
  }

  async connect(): Promise<void> {
    const nodemailer = await import("nodemailer");
    const Imap = (await import("imap")).default;

    this.transporter = nodemailer.createTransport({
      host: this.smtpHost, port: this.smtpPort, secure: this.smtpPort === 465,
      auth: { user: this.emailAddress, pass: this.password },
    });

    this.imapClient = new Imap({
      user: this.emailAddress, password: this.password,
      host: this.imapHost, port: 993, tls: true,
    });

    await new Promise<void>((resolve, reject) => {
      this.imapClient.once("ready", () => resolve());
      this.imapClient.once("error", (err: Error) => reject(err));
      this.imapClient.connect();
    });

    this.pollTimer = setInterval(() => this.poll(), this.pollInterval);
    this.connected = true;
  }

  async disconnect(): Promise<void> {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.imapClient?.end();
    this.transporter?.close?.();
    this.connected = false;
  }

  onMessage(handler: (msg: UnifiedMessage) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    const info = await this.transporter.sendMail({
      from: this.emailAddress, to: msg.chatId, subject: msg.metadata?.subject ?? "",
      text: msg.text, html: msg.metadata?.html as string | undefined,
    });
    this.lastActivity = new Date();
    return info.messageId;
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "email", accountId: this.emailAddress, lastActivity: this.lastActivity };
  }

  private poll(): void {
    if (!this.handler) return;
    this.imapClient.openBox("INBOX", true, (err: Error | null) => {
      if (err) return;
      this.imapClient.search(["UNSEEN"], (err2: Error | null, uids: number[]) => {
        if (err2 || !uids?.length) return;
        const fetch = this.imapClient.fetch(uids, { bodies: "HEADER.FIELDS (FROM SUBJECT DATE)", markSeen: true });
        fetch.on("message", (imapMsg: any) => {
          imapMsg.on("body", (stream: any) => {
            let buf = "";
            stream.on("data", (chunk: Buffer) => { buf += chunk.toString(); });
            stream.on("end", () => {
              this.msgCounter++;
              this.lastActivity = new Date();
              const from = buf.match(/From:\s*(.+)/i)?.[1]?.trim() ?? "unknown";
              const subject = buf.match(/Subject:\s*(.+)/i)?.[1]?.trim() ?? "";
              this.handler!({
                id: String(this.msgCounter), channel: "email",
                sender: { id: from, displayName: from },
                content: { type: ContentType.TEXT, text: subject },
                timestamp: new Date(), chatId: from, raw: { headers: buf },
              });
            });
          });
        });
      });
    });
  }
}
