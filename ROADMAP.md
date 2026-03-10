# Roadmap

> Position: The communication layer for personal AI assistants — IM, Email, Voice, Calendar, IoT in one unified API.

## New Adapters

### P0 — Must Have
- [ ] **Email** (Gmail API + generic IMAP/SMTP) — personal assistant #1 need
- [ ] **Twilio Voice** — phone calls (inbound + outbound, STT/TTS)
- [ ] **SMS** (Twilio) — reach anyone without an app

### P1 — High Value
- [ ] **Google Calendar** — schedule events, receive reminders as UnifiedMessage
- [ ] **Outlook Calendar** (Microsoft Graph)
- [ ] **Notion** — knowledge management gateway
- [ ] **Apple Shortcuts** — iOS ecosystem entry point

### P2 — Nice to Have
- [ ] **HomeAssistant** — smart home control
- [ ] **SIP Protocol** — raw voice calls without Twilio
- [ ] **Outlook Email** (Microsoft Graph)

## Core Features

### P0
- [ ] **Cross-Channel Relay Middleware** — forward messages between channels ("send this Telegram message to Slack")
- [ ] **Persistent Message Queue** — at-least-once delivery, survives restarts (SQLite-backed)
- [ ] **Multi-Identity Support** — multiple accounts per channel, message routing by identity

### P1
- [ ] **Rich Media Normalization** — unified attachment model, cross-platform auto-transcoding
- [ ] **Voice Message Auto-Transcription** — STT on incoming voice messages
- [ ] **Image Auto-Description** — OCR/vision on incoming images

### P2
- [ ] **Message Aggregation Middleware** — digest/summary of messages across channels
- [ ] **Priority Inbox** — LLM-ranked message importance

## Quality & Infrastructure

- [ ] Integration tests with real API mocks (testcontainers)
- [ ] Cross-language conformance test suite (shared JSON fixtures)
- [ ] Benchmark suite (throughput, latency, memory per adapter)
- [ ] Docs site (MkDocs / GitHub Pages)
- [ ] Demo video in README
- [ ] Live interactive demo (WebSocket)
- [ ] 10+ good-first-issue labels for contributors
- [ ] More examples (15+ common scenarios)

## Ecosystem — Sibling Projects

- [ ] **Personal Memory System** — long-term episodic/semantic/procedural memory, embeddable
- [ ] **Action Hub** — Calendar + Email + Files connectors, MCP Server
