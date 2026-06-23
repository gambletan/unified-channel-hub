# unified-support Docker Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package `unified-support` as a portable Docker image + single-service compose stack that also replaces the systemd deployment on `alvinclub.xyz`.

**Architecture:** Multi-stage Dockerfile (build context = repo root) builds `unified-channel` and `unified-support` wheels from monorepo source, installs them into a slim runtime with the `telegram,whatsapp` extras. The app reads a mounted `config.yaml`, takes secrets from env, persists sqlite to a named volume, and binds only to localhost behind a reverse proxy. Compose runs one service; an optional `edge` profile adds Caddy for hosts with no proxy.

**Tech Stack:** Docker, docker-compose, python:3.12-slim, hatchling wheels, Caddy (optional), nginx (existing on prod).

**Reference spec:** `docs/superpowers/specs/2026-06-11-docker-deployment-design.md`

**Working directory:** repo root `unified-channel-hub/`. All paths below are relative to it.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `support/Dockerfile` | Multi-stage build of the service image |
| `.dockerignore` | Keep build context small + secret/state-free |
| `support/vendor/.gitkeep` | Ensure the (optional) ERP bundle dir always exists for `COPY` |
| `support/scripts/bundle-erp.sh` | Copy optional `erp_client.py` from sibling `X-Auto` into the build context |
| `docker-compose.yml` | Single `support` service + optional `caddy` (profile `edge`) |
| `deploy/.env.example` | Documented list of secret env vars |
| `deploy/Caddyfile.example` | Reverse-proxy config for the `edge` profile |
| `deploy/README.md` | Build/run + systemd→Docker migration runbook |

Note `.gitignore` already ignores `*.db`, `data/`, `dist/`, `build/`, `__pycache__/`. We add `support/vendor/erp_client.py` (bundled artifact) to ignore so it never gets committed.

---

## Task 1: Build context hygiene + ERP bundle scaffolding

**Files:**
- Create: `.dockerignore`
- Create: `support/vendor/.gitkeep`
- Create: `support/scripts/bundle-erp.sh`
- Modify: `support/.gitignore` (append `vendor/erp_client.py`)

- [ ] **Step 1: Create `.dockerignore`**

```
# .dockerignore — keep build context small, secret-free, state-free
**/.venv
**/__pycache__
**/*.pyc
**/*.egg-info
**/.pytest_cache
**/dist
**/build
.git
.github
# language dirs not needed for the support image
typescript
java
rust
mcp-server
# runtime state / secrets / artifacts — never in the image
support/data
support/logs
support/config.yaml
support/.env
**/*.db
**/*.db-*
**/*.tar.gz
support/bridges/whatsapp-web/node_modules
```

- [ ] **Step 2: Create the ERP vendor dir placeholder**

```bash
mkdir -p support/vendor
: > support/vendor/.gitkeep
```

- [ ] **Step 3: Create `support/scripts/bundle-erp.sh`**

```bash
#!/usr/bin/env bash
# Copy the optional ERP client from the sibling X-Auto repo into the build context.
# ERP is optional: if the source is missing, the image still builds and ERP stays disabled.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # support/scripts -> repo root
SRC="${ERP_SRC:-$REPO_ROOT/../X-Auto/AC-Customer-Support/erp_client.py}"
DEST="$REPO_ROOT/support/vendor/erp_client.py"
if [ -f "$SRC" ]; then
  cp "$SRC" "$DEST"
  echo "bundled erp_client.py from $SRC"
else
  echo "WARN: $SRC not found — building without ERP (it will be disabled at runtime)"
fi
```

- [ ] **Step 4: Make it executable and ignore the bundled artifact**

```bash
chmod +x support/scripts/bundle-erp.sh
printf '\n# bundled at build time, never commit\nvendor/erp_client.py\n' >> support/.gitignore
```

- [ ] **Step 5: Verify the script runs (with X-Auto present) and is a no-op-safe otherwise**

Run: `support/scripts/bundle-erp.sh && ls support/vendor/`
Expected: prints `bundled erp_client.py from ...` and `vendor/` lists `.gitkeep` and `erp_client.py` (or, if X-Auto is absent, prints the WARN line and exits 0).

- [ ] **Step 6: Commit**

```bash
git add .dockerignore support/vendor/.gitkeep support/scripts/bundle-erp.sh support/.gitignore
git commit -m "build: dockerignore + optional ERP bundle scaffolding"
```

---

## Task 2: The multi-stage Dockerfile

**Files:**
- Create: `support/Dockerfile`

- [ ] **Step 1: Create `support/Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1
# Build context MUST be the repo root: `docker build -f support/Dockerfile .`

# ---- build stage: produce wheels from monorepo source ----
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir --upgrade pip wheel
# unified-channel library (the /tmp editable failure mode dies here — baked from source)
COPY python/ ./python/
# unified-support app (only what hatchling needs to build the wheel)
COPY support/pyproject.toml support/README.md ./support/
COPY support/support/ ./support/support/
RUN pip wheel --no-deps -w /wheels ./python ./support

# ---- runtime stage: slim, no build tools ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/vendor
WORKDIR /app
COPY --from=build /wheels /wheels
# Install the library WITH the channel extras, plus the app (pulls aiosqlite/aiohttp/httpx).
RUN UC="$(ls /wheels/unified_channel-*.whl)" \
 && pip install --no-cache-dir "${UC}[telegram,whatsapp]" /wheels/unified_support-*.whl \
 && rm -rf /wheels
# Optional ERP client (top-level module on PYTHONPATH; absent => ERP disabled, app handles it)
COPY support/vendor/ /app/vendor/
# Default knowledge base — a mounted volume can override /app/knowledge
COPY support/knowledge/ /app/knowledge/
RUN useradd -m app \
 && mkdir -p /app/config /app/data \
 && chown -R app:app /app
USER app
EXPOSE 8081 8082 8443
# Liveness = dashboard port is accepting connections (auth-agnostic; /support/ returns 401 when healthy)
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(('127.0.0.1',8081)))"
ENTRYPOINT ["python", "-m", "support.app", "/app/config/config.yaml"]
```

- [ ] **Step 2: Commit**

```bash
git add support/Dockerfile
git commit -m "build: multi-stage Dockerfile for unified-support service image"
```

---

## Task 3: Build the image and verify it imports

**Files:** none (verification task)

- [ ] **Step 1: Bundle ERP (optional) then build**

Run:
```bash
support/scripts/bundle-erp.sh
docker build -f support/Dockerfile -t unified-support:latest .
```
Expected: build completes with `naming to docker.io/library/unified-support:latest` (or equivalent), no errors.

- [ ] **Step 2: Verify the app imports inside the image**

Run:
```bash
docker run --rm --entrypoint python unified-support:latest -c "import support.app, unified_channel; print('imports OK')"
```
Expected: prints `imports OK` and exits 0.

- [ ] **Step 3: Verify the channel extras are present**

Run:
```bash
docker run --rm --entrypoint python unified-support:latest -c "import telegram, httpx; print('telegram+httpx OK')"
```
Expected: prints `telegram+httpx OK`.

- [ ] **Step 4: Verify no secrets baked in**

Run:
```bash
docker run --rm --entrypoint sh unified-support:latest -c "test ! -f /app/config/config.yaml && test ! -f /app/.env && echo 'no secrets/config baked'"
```
Expected: prints `no secrets/config baked`.

(No commit — verification only.)

---

## Task 4: Compose stack + config dir + env template

**Files:**
- Create: `docker-compose.yml`
- Create: `deploy/.env.example`
- Create: `deploy/config/.gitkeep`

- [ ] **Step 1: Create `docker-compose.yml`**

```yaml
services:
  support:
    build:
      context: .
      dockerfile: support/Dockerfile
    image: unified-support:latest
    restart: unless-stopped
    env_file: deploy/.env            # secrets only
    volumes:
      - ./deploy/config:/app/config:ro      # config.yaml lives here (editable, no rebuild)
      - support-data:/app/data              # sqlite support.db persists
      - ./support/knowledge:/app/knowledge:ro  # optional: override baked FAQ
    ports:
      - "127.0.0.1:8081:8081"   # dashboard
      - "127.0.0.1:8082:8082"   # webchat ws
      - "127.0.0.1:8443:8443"   # whatsapp webhook

  # Optional: only for fresh hosts with no existing reverse proxy.
  #   docker compose --profile edge up -d
  caddy:
    profiles: ["edge"]
    image: caddy:2
    restart: unless-stopped
    network_mode: host                 # so it can reach 127.0.0.1:8081/8082/8443
    volumes:
      - ./deploy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    depends_on: [support]

volumes:
  support-data: {}
  caddy-data: {}
```

- [ ] **Step 2: Create `deploy/.env.example`**

```
# Secrets for unified-support. Copy to deploy/.env and fill in. NEVER commit deploy/.env.
TELEGRAM_BOT_TOKEN=
AI_API_KEY=
# WhatsApp official Cloud API
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_VERIFY_TOKEN=
# ERP (optional — leave blank to disable)
ERP_BASE_URL=
ERP_API_KEY=
```

- [ ] **Step 3: Create the config dir placeholder and ignore real secrets**

```bash
mkdir -p deploy/config
: > deploy/config/.gitkeep
printf '\n# docker deploy secrets/config\ndeploy/.env\ndeploy/config/config.yaml\n' >> .gitignore
```

- [ ] **Step 4: Verify compose config is valid**

Run: `docker compose config >/dev/null && echo "compose valid"`
Expected: prints `compose valid` (no schema errors).

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml deploy/.env.example deploy/config/.gitkeep .gitignore
git commit -m "build: docker-compose stack + env template + config mount"
```

---

## Task 5: Boot + health verification with a sample config

**Files:** none (verification task; uses a throwaway local config)

- [ ] **Step 1: Stage a minimal config + dummy env**

Run:
```bash
cp support/config.example.yaml deploy/config/config.yaml
cp deploy/.env.example deploy/.env
# put a syntactically-valid dummy telegram token so the app starts its poller
sed -i.bak 's/^TELEGRAM_BOT_TOKEN=$/TELEGRAM_BOT_TOKEN=123456:dummy/' deploy/.env && rm -f deploy/.env.bak
```

- [ ] **Step 2: Bring up the service**

Run: `docker compose up -d support && sleep 12`
Expected: container `support` is `Up`.

- [ ] **Step 3: Verify the dashboard port is live (healthcheck basis)**

Run:
```bash
docker compose exec -T support python -c "import socket;s=socket.socket();s.settimeout(3);print('8081 open' if s.connect_ex(('127.0.0.1',8081))==0 else '8081 DOWN')"
```
Expected: prints `8081 open`.

- [ ] **Step 4: Verify all three listeners bound + channels started in logs**

Run: `docker compose logs support | grep -E "unified-channel started|webchat listening|whatsapp connected"`
Expected: lines showing `channels=['telegram', 'webchat', 'whatsapp']`, webchat listening on 8082, whatsapp webhook on 8443.

- [ ] **Step 5: Tear down (keep volume)**

Run: `docker compose down`
Expected: container removed; `support-data` volume remains.

(No commit — verification only.)

---

## Task 6: Persistence + config-override + no-secrets verification

**Files:** none (verification task)

- [ ] **Step 1: Write a row, restart, confirm it survives the volume**

Run:
```bash
docker compose up -d support && sleep 8
docker compose exec -T support python -c "
import asyncio; from support.db import Database; from support.models import Ticket
async def m():
    d=Database('/app/data/support.db'); await d.connect()
    await d.create_ticket(Ticket(channel='webchat', chat_id='persist-test', customer_id='u1'))
    await d.close()
asyncio.run(m())"
docker compose restart support && sleep 8
docker compose exec -T support python -c "
import asyncio; from support.db import Database
async def m():
    d=Database('/app/data/support.db'); await d.connect()
    t=await d.find_ticket_by_chat('webchat','persist-test')
    print('PERSISTED' if t else 'LOST'); await d.close()
asyncio.run(m())"
```
Expected: prints `PERSISTED`.

- [ ] **Step 2: Verify config edits take effect with no rebuild**

Run:
```bash
sed -i.bak 's/max_ai_turns: 8/max_ai_turns: 3/' deploy/config/config.yaml && rm -f deploy/config/config.yaml.bak
docker compose restart support && sleep 8
docker compose exec -T support python -c "
from support.app import load_support_config
print('max_ai_turns =', load_support_config('/app/config/config.yaml')['escalation']['max_ai_turns'])"
```
Expected: prints `max_ai_turns = 3` (the mounted file changed; image did not).

- [ ] **Step 3: Confirm image carries no secrets in its layers**

Run: `docker history --no-trunc unified-support:latest | grep -iE "token|api_key|secret" || echo "no secret strings in image history"`
Expected: prints `no secret strings in image history`.

- [ ] **Step 4: Clean up the throwaway state**

Run: `docker compose down -v && rm -f deploy/config/config.yaml deploy/.env`
Expected: containers + `support-data` volume removed; local throwaway config/env deleted.

(No commit — verification only.)

---

## Task 7: Caddy edge profile config

**Files:**
- Create: `deploy/Caddyfile.example`

- [ ] **Step 1: Create `deploy/Caddyfile.example`**

```
# Copy to deploy/Caddyfile and set your domain. Used only with `--profile edge`
# on fresh hosts that have no existing reverse proxy (Caddy auto-provisions HTTPS,
# which the WhatsApp Cloud API webhook requires).
your-domain.example {
    @webhook path /whatsapp/webhook*
    handle @webhook { reverse_proxy 127.0.0.1:8443 }

    handle /ws*       { reverse_proxy 127.0.0.1:8082 }
    handle /support*  { reverse_proxy 127.0.0.1:8081 }
    handle            { reverse_proxy 127.0.0.1:8081 }
}
```

- [ ] **Step 2: Verify Caddyfile syntax via the Caddy image**

Run:
```bash
cp deploy/Caddyfile.example deploy/Caddyfile
docker run --rm -v "$PWD/deploy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && rm -f deploy/Caddyfile
```
Expected: `Valid configuration` (the placeholder domain validates fine).

- [ ] **Step 3: Commit**

```bash
git add deploy/Caddyfile.example
git commit -m "build: optional Caddy edge profile config"
```

---

## Task 8: Deploy runbook (build/run + systemd→Docker migration)

**Files:**
- Create: `deploy/README.md`

- [ ] **Step 1: Create `deploy/README.md`**

````markdown
# unified-support — Docker deployment

## Build & run (any host)

```bash
# 1. (optional) bundle the ERP client from the sibling X-Auto repo
support/scripts/bundle-erp.sh

# 2. build
docker build -f support/Dockerfile -t unified-support:latest .

# 3. configure
cp deploy/.env.example deploy/.env                 # fill in secrets
cp support/config.example.yaml deploy/config/config.yaml   # edit prompt/agents/topic_bridge

# 4. run (binds to 127.0.0.1 — put a reverse proxy in front)
docker compose up -d
```

Fresh host with no proxy? Add TLS via Caddy:
```bash
cp deploy/Caddyfile.example deploy/Caddyfile   # set your domain
docker compose --profile edge up -d
```

## ⚠️ One bot, one instance

Telegram allows a single `getUpdates` poller per bot token. Run **only one**
compose stack per token, and never share the production token with a dev/local
run — two pollers crash each other with `telegram.error.Conflict`.

## Migrating alvinclub.xyz from systemd to Docker

The existing nginx config is untouched — the container listens on the same
localhost ports systemd used. Steps on the server:

```bash
cd ~/unified-support-deploy

# 1. carry over config, secrets, knowledge, and the live DB
mkdir -p /opt/unified-support/deploy/config
cp config.yaml          /opt/unified-support/deploy/config/config.yaml
cp .env                 /opt/unified-support/deploy/.env        # existing secrets
# (point the support-data volume at the current DB — see step 3)

# 2. stop the systemd instance (keep the unit for rollback)
sudo systemctl stop unified-support
sudo systemctl disable unified-support

# 3. seed the data volume with the current sqlite DB, then start
docker compose up -d --no-start
docker run --rm -v unified-channel-hub_support-data:/dest -v "$PWD":/src busybox \
  sh -c "cp /src/data/support.db /dest/support.db"
docker compose up -d

# 4. verify
docker compose ps
docker compose logs support | grep "unified-channel started"
curl -s -o /dev/null -w '%{http_code}\n' https://alvinclub.xyz/support/   # expect 401 = healthy
docker compose logs support | grep -i conflict || echo "no telegram conflict — good"
```

Rollback: `docker compose down && sudo systemctl enable --now unified-support`.
````

- [ ] **Step 2: Commit**

```bash
git add deploy/README.md
git commit -m "docs: docker build/run + systemd-to-docker migration runbook"
```

---

## Self-review notes (resolved)

- **Spec coverage:** image (Task 2-3), config/secrets mount (Task 4), persistence (Task 6),
  networking/localhost binding (Task 4), Caddy edge profile (Task 7), single-instance guardrail
  + migration (Task 8), library-from-source + ERP bundling (Tasks 1-2). All spec sections mapped.
- **Healthcheck:** uses a TCP port-open check (not `curl`, absent in slim; not HTTP 200, since
  `/support/` returns 401 when healthy).
- **ERP optionality:** `COPY support/vendor/` always succeeds (`.gitkeep` keeps the dir);
  `erp_client.py` is bundled only if X-Auto is present, and the app degrades gracefully otherwise.
- **Open item carried from spec:** the root `Dockerfile` stub (library-only) is left as-is;
  folding it in is out of scope for this plan.
