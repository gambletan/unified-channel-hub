# unified-support — Docker deployment

## Build & run (any host)

```bash
# 1. (optional) bundle the ERP client from the sibling X-Auto repo
support/scripts/bundle-erp.sh

# 2. build
docker build -f support/Dockerfile -t unified-support:latest .

# 3. configure
cp deploy/.env.example deploy/.env                          # fill in secrets
cp support/config.example.yaml deploy/config/config.yaml    # edit prompt/agents/topic_bridge

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
run — two pollers crash each other with `telegram.error.Conflict`. (Also note: an
*invalid* Telegram token currently crashes the app on startup — make sure the
token in `deploy/.env` is valid.)

## Config & secrets

- `deploy/config/config.yaml` — non-secret structure (prompt, agents, topic-bridge,
  model routing). Mounted read-only; edit + `docker compose restart support` to apply,
  no rebuild.
- `deploy/.env` — secrets only (`TELEGRAM_BOT_TOKEN`, `AI_API_KEY`, `GEMINI_API_KEY`,
  `WHATSAPP_*`, `ERP_*`). The image contains no secrets.
- sqlite `support.db` lives in the `support-data` named volume (survives restarts/upgrades).

## Migrating alvinclub.xyz from systemd to Docker

The existing nginx config is untouched — the container listens on the same localhost
ports systemd used. On the server:

```bash
cd ~/unified-support-deploy

# 1. stage a compose dir with the current config/secrets/knowledge
mkdir -p /opt/unified-support/deploy/config
cp config.yaml /opt/unified-support/deploy/config/config.yaml
cp .env        /opt/unified-support/deploy/.env

# 2. stop the systemd instance (keep the unit + restore-library-patches.sh for rollback)
sudo systemctl stop unified-support && sudo systemctl disable unified-support

# 3. seed the data volume with the live DB, then start
docker compose up -d --no-start
docker run --rm -v unified-support_support-data:/dest -v "$PWD":/src busybox \
  sh -c "cp /src/data/support.db /dest/support.db"
docker compose up -d

# 4. verify
docker compose ps
curl -s -o /dev/null -w '%{http_code}\n' https://alvinclub.xyz/support/   # expect 401 = healthy
docker compose logs support | grep -i conflict || echo "no telegram conflict — good"
```

Rollback: `docker compose down && sudo systemctl enable --now unified-support`.

## Why Docker (vs the systemd deploy)

The image bakes `unified_channel` from monorepo source, so the
hand-patched site-packages files (and their `restore-library-patches.sh` band-aid)
go away — the build is reproducible and reinstall-proof. PyYAML is now a declared
dependency (the systemd deploy only had it transitively).
