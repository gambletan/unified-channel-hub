/** Google Calendar adapter — event polling + creation via googleapis. */

import type { ChannelAdapter } from "../adapter.js";
import { ContentType, type ChannelStatus, type OutboundMessage, type UnifiedMessage } from "../types.js";

export class GoogleCalendarAdapter implements ChannelAdapter {
  readonly channelId = "google_calendar";
  private connected = false;
  private lastActivity?: Date;
  private handler?: (msg: UnifiedMessage) => void;
  private msgCounter = 0;
  private pollTimer?: ReturnType<typeof setInterval>;
  private calendar: any;
  private calendarId: string;
  private pollInterval: number;
  private lastSync?: string; // RFC3339 for incremental polling

  constructor(
    private credentialsPath: string,
    private options: { calendarId?: string; pollInterval?: number } = {}
  ) {
    this.calendarId = options.calendarId ?? "primary";
    this.pollInterval = options.pollInterval ?? 60_000;
  }

  async connect(): Promise<void> {
    const { google } = await import("googleapis");
    const fs = await import("node:fs/promises");

    const creds = JSON.parse(await fs.readFile(this.credentialsPath, "utf-8"));
    const auth = new google.auth.GoogleAuth({ credentials: creds, scopes: ["https://www.googleapis.com/auth/calendar"] });
    this.calendar = google.calendar({ version: "v3", auth });

    this.lastSync = new Date().toISOString();
    this.pollTimer = setInterval(() => this.poll(), this.pollInterval);
    this.connected = true;
  }

  async disconnect(): Promise<void> {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.connected = false;
  }

  onMessage(handler: (msg: UnifiedMessage) => void): void { this.handler = handler; }

  async send(msg: OutboundMessage): Promise<string | undefined> {
    const meta = msg.metadata ?? {};
    const event = await this.calendar.events.insert({
      calendarId: this.calendarId,
      requestBody: {
        summary: msg.text,
        start: { dateTime: meta.startTime as string, timeZone: (meta.timeZone as string) ?? "UTC" },
        end: { dateTime: meta.endTime as string, timeZone: (meta.timeZone as string) ?? "UTC" },
        description: meta.description as string | undefined,
        location: meta.location as string | undefined,
      },
    });
    this.lastActivity = new Date();
    return event.data.id;
  }

  async getStatus(): Promise<ChannelStatus> {
    return { connected: this.connected, channel: "google_calendar", accountId: this.calendarId, lastActivity: this.lastActivity };
  }

  private async poll(): Promise<void> {
    if (!this.handler) return;
    try {
      const res = await this.calendar.events.list({
        calendarId: this.calendarId, timeMin: this.lastSync, singleEvents: true,
        orderBy: "startTime", maxResults: 20,
      });
      this.lastSync = new Date().toISOString();
      for (const event of res.data.items ?? []) {
        this.msgCounter++;
        this.lastActivity = new Date();
        this.handler({
          id: event.id ?? String(this.msgCounter), channel: "google_calendar",
          sender: { id: event.creator?.email ?? "unknown", displayName: event.creator?.displayName },
          content: { type: ContentType.TEXT, text: event.summary ?? "(no title)" },
          timestamp: new Date(event.start?.dateTime ?? event.start?.date ?? Date.now()),
          chatId: this.calendarId, raw: event,
          metadata: { startTime: event.start?.dateTime, endTime: event.end?.dateTime, location: event.location },
        });
      }
    } catch { /* poll failure is non-fatal */ }
  }
}
