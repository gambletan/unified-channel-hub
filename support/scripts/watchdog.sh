#!/usr/bin/env bash
# unified-support watchdog — health probe + auto-restart + Telegram alert.
# Installed via cron (every few minutes). Logs to logs/watchdog.log.
# Complements systemd Restart=always: catches "alive but broken" + crash-loops
# (e.g. the Telegram dual-instance Conflict or a missing-module loop) that
# systemd cannot fix on its own, and alerts a human.
set -uo pipefail

DEPLOY="$HOME/unified-support-deploy"
LOGDIR="$DEPLOY/logs"
LOG="$LOGDIR/watchdog.log"
STATE="$LOGDIR/watchdog.state"          # ok | bad
NR_STATE="$LOGDIR/watchdog.nrestarts"   # last seen systemd NRestarts
UNIT="unified-support"
GROUP_CHAT="-1003828541886"             # CS agent group (alert target)
mkdir -p "$LOGDIR"

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }

alert(){  # $1 = text — reads bot token from config.yaml at runtime (never logged)
  local tok
  tok="$(cd "$DEPLOY" && .venv/bin/python -c "import yaml;print(yaml.safe_load(open('config.yaml'))['channels']['telegram']['token'])" 2>/dev/null)"
  [ -z "$tok" ] && { log "alert skipped (no telegram token in config.yaml)"; return; }
  curl -s -m 10 "https://api.telegram.org/bot${tok}/sendMessage" \
       --data-urlencode chat_id="$GROUP_CHAT" \
       --data-urlencode text="$1" >/dev/null 2>&1 || log "alert send failed"
}

# ---- probe ----
problems=()
active="$(systemctl is-active "$UNIT" 2>/dev/null || true)"
[ "$active" = "active" ] || problems+=("systemd=$active")
for p in 8081 8082 8443; do
  ss -ltn 2>/dev/null | grep -q ":$p " || problems+=("port${p}-down")
done

# crash-loop detection: NRestarts climbing fast between runs
now_nr="$(systemctl show "$UNIT" -p NRestarts --value 2>/dev/null || echo 0)"
prev_nr="$(cat "$NR_STATE" 2>/dev/null || echo "$now_nr")"
echo "$now_nr" > "$NR_STATE"
delta=$(( now_nr - prev_nr ))
[ "$delta" -ge 3 ] && problems+=("crash-loop(+${delta})")

prev="$(cat "$STATE" 2>/dev/null || echo ok)"

# keep the log bounded
[ -f "$LOG" ] && tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"

# ---- healthy ----
if [ "${#problems[@]}" -eq 0 ]; then
  if [ "$prev" != ok ]; then
    log "RECOVERED — healthy"
    alert "✅ [监控] 客服服务已恢复正常 (alvinclub.xyz)"
  else
    log "OK (nr=${now_nr})"   # heartbeat: confirms cron is firing
  fi
  echo ok > "$STATE"
  exit 0
fi

joined="$(IFS=,; echo "${problems[*]}")"
log "UNHEALTHY: $joined"

# crash-looping: systemd is already restarting; fighting it won't help → alert only
if printf '%s' "$joined" | grep -q crash-loop; then
  [ "$prev" = ok ] && alert "🔴 [监控] 客服服务崩溃重启循环: ${joined} — systemd 自动重启无效，需人工介入 (alvinclub.xyz)"
  echo bad > "$STATE"
  exit 0
fi

# down / ports closed: try one restart, then re-probe
log "restarting ${UNIT}"
sudo systemctl restart "$UNIT" 2>>"$LOG"
sleep 8
ok=1
[ "$(systemctl is-active "$UNIT" 2>/dev/null)" = active ] || ok=0
for p in 8081 8082 8443; do ss -ltn 2>/dev/null | grep -q ":$p " || ok=0; done
systemctl show "$UNIT" -p NRestarts --value 2>/dev/null > "$NR_STATE"  # rebaseline after our restart
if [ "$ok" -eq 1 ]; then
  log "restart OK — back up"
  [ "$prev" = ok ] && alert "⚠️ [监控] 客服服务异常已自动重启并恢复: ${joined} (alvinclub.xyz)"
  echo ok > "$STATE"
else
  log "restart FAILED — still unhealthy"
  alert "🔴 [监控] 客服服务异常且自动重启失败: ${joined} — 需人工介入 (alvinclub.xyz)"
  echo bad > "$STATE"
fi
