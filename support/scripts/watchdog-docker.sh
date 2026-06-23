#!/usr/bin/env bash
# unified-support DOCKER watchdog — health probe + restart + Telegram alert.
# Monitors the compose container (replaces the systemd watchdog after the Docker cutover).
# Compose's restart:unless-stopped handles plain crashes; this catches "alive but
# unhealthy" + crash-loops and alerts a human. Run hourly via cron.
set -uo pipefail

COMPOSE_DIR="$HOME/uch-build"
CONTAINER="uch-build-support-1"
CONFIG="$COMPOSE_DIR/deploy/config/config.yaml"
PY="$HOME/unified-support-deploy/.venv/bin/python"   # has pyyaml; reads the token at runtime
LOGDIR="$COMPOSE_DIR/logs"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/watchdog.log"; STATE="$LOGDIR/watchdog.state"; NR_STATE="$LOGDIR/watchdog.nr"
GROUP_CHAT="-1003828541886"

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }

alert(){  # token read from config.yaml at runtime (never logged)
  local tok
  tok="$("$PY" -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['channels']['telegram']['token'])" 2>/dev/null)"
  [ -z "$tok" ] && { log "alert skipped (no telegram token)"; return; }
  curl -s -m 10 "https://api.telegram.org/bot${tok}/sendMessage" \
       --data-urlencode chat_id="$GROUP_CHAT" --data-urlencode text="$1" >/dev/null 2>&1 || log "alert send failed"
}

port_down(){ local p="$1" i; for i in 1 2 3; do
  (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && { exec 3>&- 3<&- 2>/dev/null; return 1; }; sleep 2; done; return 0; }

# ---- probe ----
problems=()
state="$(docker inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
health="$(docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
[ "$state" = "running" ] || problems+=("container=$state")
[ -n "$health" ] && [ "$health" != "healthy" ] && [ "$health" != "starting" ] && problems+=("health=$health")
for p in 8081 8082 8443; do port_down "$p" && problems+=("port${p}-down"); done

now_nr="$(docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null || echo 0)"
prev_nr="$(cat "$NR_STATE" 2>/dev/null || echo "$now_nr")"; echo "$now_nr" > "$NR_STATE"
delta=$(( now_nr - prev_nr )); [ "$delta" -ge 3 ] && problems+=("crash-loop(+${delta})")

[ -f "$LOG" ] && tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
prev="$(cat "$STATE" 2>/dev/null || echo ok)"

# ---- healthy ----
if [ "${#problems[@]}" -eq 0 ]; then
  if [ "$prev" != ok ]; then log "RECOVERED"; alert "✅ [监控] 客服服务已恢复正常 (alvinclub.xyz, docker)"
  else log "OK (nr=${now_nr} health=${health:-none})"; fi
  echo ok > "$STATE"; exit 0
fi

joined="$(IFS=,; echo "${problems[*]}")"; log "UNHEALTHY: $joined"
if printf '%s' "$joined" | grep -q crash-loop; then
  [ "$prev" = ok ] && alert "🔴 [监控] 客服容器崩溃重启循环: ${joined} — restart:unless-stopped 救不了，需人工介入 (alvinclub.xyz)"
  echo bad > "$STATE"; exit 0
fi

log "restarting container"
( cd "$COMPOSE_DIR" && docker compose restart support ) >>"$LOG" 2>&1
sleep 10
ok=1
[ "$(docker inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null)" = running ] || ok=0
for p in 8081 8082 8443; do (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && { exec 3>&- 3<&- 2>/dev/null; } || ok=0; done
docker inspect "$CONTAINER" --format '{{.RestartCount}}' 2>/dev/null > "$NR_STATE"
if [ "$ok" -eq 1 ]; then
  log "restart OK"; [ "$prev" = ok ] && alert "⚠️ [监控] 客服容器异常已自动重启并恢复: ${joined} (alvinclub.xyz)"; echo ok > "$STATE"
else
  log "restart FAILED"; alert "🔴 [监控] 客服容器异常且自动重启失败: ${joined} — 需人工介入 (alvinclub.xyz)"; echo bad > "$STATE"
fi
