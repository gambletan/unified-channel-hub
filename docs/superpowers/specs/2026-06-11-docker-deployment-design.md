# unified-support Docker Deployment — Design

**Status:** Approved design, pending spec review
**Date:** 2026-06-11

## Goal

Package the `unified-support` customer-service system as a Docker image that is both:

1. **Portable** — deployable to any host or cloud with one command, and
2. **A drop-in replacement** for the current systemd deployment on `alvinclub.xyz`.

A secondary goal is to permanently eliminate the failure mode that took prod
down on 2026-06-10: `unified-channel` was an *editable* install pointing at
`/tmp/unified-channel-src`, which a host reboot wiped, crashing the app with
`ModuleNotFoundError`. Baking the library into an immutable image removes that
class of problem.

## Background / current state

- **Monorepo layout.** `python/` is the `unified-channel` library (gateway, per-channel
  optional-dependency extras). `support/` is the `unified-support` app, which depends on
  `unified-channel>=0.2.0`.
- **Entry point:** `python -m support.app config.yaml`.
- **Channels:** Telegram (long-poll), WebChat (WS :8082), WhatsApp **official Meta Cloud API**
  webhook (:8443), dashboard (:8081).
- **Config:** `config.yaml` mixes non-secret structure (system prompt, agent roster,
  topic-bridge group IDs) with `${ENV}` interpolation for secrets, sourced from a `.env`.
- **State:** sqlite `data/support.db`; a `knowledge/` dir (FAQ), reindexed on start.
- **ERP client:** optional. `support/app.py` loads a top-level `erp_client` module sourced
  from a sibling repo `X-Auto/AC-Customer-Support/erp_client.py`; wrapped in try/except and
  only used when `erp.base_url` is set.
- **Today:** runs under systemd (`unified-support.service`), support package editable-installed
  from `~/unified-support-deploy`, fronted by host nginx.

## Decisions (locked)

| Area | Decision |
|------|----------|
| Image scope | **Single Python image.** WhatsApp official Cloud API only — no whatsapp-web.js node bridge (would need chromium + QR session). |
| Library source | Build `unified-channel` **from monorepo `python/` source**, not PyPI — roots out the `/tmp` editable problem and stays in sync with our code. |
| Config / secrets | **`config.yaml` mounted read-only**; secrets injected via env (`.env` / docker secrets). Image contains no secrets and no `config.yaml`. |
| Persistence | sqlite `support.db` in a **named volume**; `knowledge/` optionally bind-mounted to override the baked default. |
| ERP client | Bundled into the image at build time from `X-Auto`. If absent at build, image still builds and ERP stays disabled. |
| Networking | Container ports bound to **`127.0.0.1`**; all external access via a reverse proxy. |
| Orchestration | **Single-service docker-compose**, `restart: unless-stopped`. Optional **`edge` profile** adds Caddy for hosts with no existing proxy. |
| Prod migration | On `alvinclub.xyz`: container listens on localhost, **existing nginx unchanged**; cut over from systemd to compose. |

## Architecture

```
                      ┌─────────────────── host ───────────────────┐
   Internet ──TLS──►  │  reverse proxy (alvinclub: existing nginx;  │
                      │  fresh host: optional Caddy `edge` profile) │
                      │            │  proxies to 127.0.0.1            │
                      │            ▼                                 │
                      │   ┌────────────────────────────┐            │
                      │   │ unified-support container   │            │
                      │   │  python -m support.app      │            │
                      │   │  :8081 dashboard            │            │
                      │   │  :8082 webchat ws           │            │
                      │   │  :8443 whatsapp webhook     │            │
                      │   │  telegram: outbound poll    │            │
                      │   └──────┬──────────┬───────────┘            │
                      │   config.yaml(:ro)  support-data (volume)    │
                      │   + .env (secrets)  knowledge/ (:ro, opt.)   │
                      └─────────────────────────────────────────────┘
```

### Component 1 — Image (multi-stage `support/Dockerfile`, build context = repo root)

```
Stage "build" (python:3.12-slim):
  COPY python/   → pip wheel  → unified_channel-*.whl
  COPY support/  → pip wheel  → unified_support-*.whl
Stage "runtime" (python:3.12-slim):
  pip install both wheels + extras: unified-channel[telegram,whatsapp]
  COPY erp_client.py   → /app  (top-level module; optional)
  COPY knowledge/      → /app/knowledge  (default FAQ; volume can override)
  non-root user, WORKDIR /app
  HEALTHCHECK: python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/')"
               (stdlib only — slim image has no curl)
  ENTRYPOINT: ["python","-m","support.app","/app/config/config.yaml"]
```

What does it do / how to use / depends on: produces an immutable image that runs the
support app; consumes a mounted `config.yaml`, a `.env`, and a data volume; depends only
on the monorepo source (no `/tmp`, no PyPI pin, no editable installs).

### Component 2 — Compose + config/secrets/persistence

```yaml
services:
  support:
    build: { context: ., dockerfile: support/Dockerfile }
    image: unified-support:latest
    restart: unless-stopped
    env_file: .env                     # secrets only: TELEGRAM_BOT_TOKEN, WHATSAPP_*, AI_API_KEY, ERP_*
    volumes:
      - ./config:/app/config:ro        # config.yaml (structure/prompt/agents — editable, no rebuild)
      - support-data:/app/data         # sqlite support.db (persists across restarts)
      - ./knowledge:/app/knowledge:ro  # optional: override baked FAQ
    ports:
      - "127.0.0.1:8081:8081"
      - "127.0.0.1:8082:8082"
      - "127.0.0.1:8443:8443"

  # optional: only for fresh hosts with no proxy — `docker compose --profile edge up`
  caddy:
    profiles: ["edge"]
    image: caddy:2
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes: [ "./Caddyfile:/etc/caddy/Caddyfile:ro", "caddy-data:/data" ]
    depends_on: [ support ]

volumes:
  support-data: {}
  caddy-data: {}
```

`config.yaml` keeps `${ENV}` interpolation; values come from `env_file`. Editing prompt/agents
is a file edit + restart, never a rebuild.

### Component 3 — Networking / TLS

- **alvinclub.xyz:** container binds localhost; **existing nginx config is untouched**
  (`/support/`→8081, webhook→8443, `/ws`→8082, etc.). Only the backend changes from a
  systemd process to a container.
- **Fresh host:** `docker compose --profile edge up` brings up Caddy for automatic HTTPS
  (needed because the WhatsApp Cloud API webhook requires a valid TLS endpoint).

### Component 4 — Single-instance guardrail

Telegram allows exactly one `getUpdates` poller per bot. Docker does not prevent double-runs.
The README must state: **run only one compose stack per bot token**, and never share the
production token with a dev/local instance (this is exactly what caused the 2026-06-10 outage).
`restart: unless-stopped` replaces systemd's self-heal.

## Data flow

1. Inbound: Telegram poll / WhatsApp webhook (:8443 via proxy) / WebChat WS (:8082 via proxy)
   → `support.app` → AI + topic-bridge → reply out the same channel.
2. State writes: tickets/messages → `support.db` on the `support-data` volume (survives
   restarts and image upgrades).
3. Config read: `config.yaml` (mounted) + env secrets at startup; `knowledge/` reindexed on boot.

## Migration plan (systemd → Docker on alvinclub.xyz)

1. Build/load the image on the host (or pull from a registry once we publish one).
2. Stage a compose dir: copy existing `config.yaml`, `.env`, `data/support.db`, `knowledge/`
   from `~/unified-support-deploy` into the compose project; seed the `support-data` volume
   with the current `support.db`.
3. `systemctl stop unified-support && systemctl disable unified-support`.
4. `docker compose up -d`.
5. Verify: all three ports up, Telegram/WhatsApp/WebChat connected, `https://alvinclub.xyz/support/`
   returns 401 (healthy), no Telegram `Conflict`.
6. Keep the systemd unit file (disabled) for ~1 week as a rollback path.

## Testing

- **Build test:** image builds from a clean checkout; `python -c "import support.app"` inside it.
- **Boot test (throwaway):** run with a sample `config.yaml` + dummy `.env`; assert dashboard
  health endpoint responds and the three listeners bind.
- **Persistence test:** write a ticket, `docker compose restart`, confirm it survives (volume).
- **Config-override test:** edit mounted `config.yaml`, restart, confirm change takes effect
  with no rebuild.
- **No-secrets test:** `docker history` / image inspection shows no tokens baked in.

## Out of scope (YAGNI)

- Unofficial whatsapp-web.js node bridge (separate sidecar image if ever needed).
- Kubernetes manifests / multi-replica (single instance is correct for the Telegram poller).
- Publishing to a container registry (can add later; initial deploy builds on-host).

## Open considerations

- **ERP client coupling:** `erp_client.py` lives in a separate repo (`X-Auto`). Build needs
  access to it; if unavailable, the image builds without ERP (feature degrades gracefully).
  Longer term, ERP should become a proper dependency rather than a copied file.
- **Existing `Dockerfile` at repo root** is a library-only stub; the new `support/Dockerfile`
  is the service image. Decide whether to keep both or fold the stub in.
