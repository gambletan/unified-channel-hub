#!/usr/bin/env python3
"""Health monitor for unified-support service.

Checks:
  1. Dashboard HTTP (port 8081) responds 200
  2. Webchat WebSocket (port 8082) accepts connections
  3. Telegram bot can call getMe

If any check fails:
  - Restart the launchd service
  - Send alert via Telegram to admin

Runs via launchd every 5 minutes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

LAUNCHD_LABEL = "com.tan.unified-support"
DASHBOARD_URL = "http://localhost:8081/"
WEBCHAT_URL = "http://localhost:8082/"
TELEGRAM_BOT_TOKEN = "7968160842:AAFeuwLkRAQR5ifd2SogX0Wj72pi07go0wY"
ALERT_CHAT_ID = "1922559342"  # Admin Telegram chat ID

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
STATE_FILE = LOG_DIR / "healthcheck_state.json"

# Cooldown: don't restart more than once per 10 minutes
RESTART_COOLDOWN_S = 600


# ── Checks ────────────────────────────────────────────────────────────────────

def check_http(url: str, timeout: int = 10) -> tuple[bool, str]:
    """Check if HTTP endpoint responds with 2xx."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 400:
                return True, f"{resp.status}"
            return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)[:100]


def check_port(host: str, port: int, timeout: int = 5) -> tuple[bool, str]:
    """Check if a TCP port is accepting connections."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "open"
    except Exception as e:
        return False, str(e)[:100]


def check_telegram_bot(timeout: int = 10) -> tuple[bool, str]:
    """Check if Telegram bot is responsive via getMe."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return True, data["result"].get("username", "ok")
            return False, data.get("description", "not ok")
    except Exception as e:
        return False, str(e)[:100]


# ── Actions ───────────────────────────────────────────────────────────────────

def send_telegram_alert(text: str):
    """Send alert message via Telegram bot."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": ALERT_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[alert] Failed to send Telegram alert: {e}", file=sys.stderr)


def restart_service() -> bool:
    """Restart the launchd service. Returns True if successful."""
    try:
        subprocess.run(
            ["launchctl", "unload",
             str(Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist")],
            capture_output=True, timeout=10,
        )
        time.sleep(2)
        subprocess.run(
            ["launchctl", "load",
             str(Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist")],
            capture_output=True, timeout=10,
        )
        time.sleep(5)
        # Verify it came back
        ok, _ = check_http(DASHBOARD_URL, timeout=5)
        return ok
    except Exception as e:
        print(f"[restart] Failed: {e}", file=sys.stderr)
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    checks = {
        "dashboard": check_http(DASHBOARD_URL),
        "webchat": check_port("127.0.0.1", 8082),
        "telegram_bot": check_telegram_bot(),
    }

    failed = {name: detail for name, (ok, detail) in checks.items() if not ok}

    if not failed:
        print(f"[{ts}] OK — all checks passed")
        # Clear consecutive fail counter
        state = load_state()
        if state.get("consecutive_fails", 0) > 0:
            state["consecutive_fails"] = 0
            save_state(state)
        return

    # Something failed
    state = load_state()
    consecutive = state.get("consecutive_fails", 0) + 1
    last_restart = state.get("last_restart_ts", 0)
    state["consecutive_fails"] = consecutive
    save_state(state)

    fail_summary = ", ".join(f"{k}: {v}" for k, v in failed.items())
    print(f"[{ts}] FAIL ({consecutive}) — {fail_summary}", file=sys.stderr)

    # Only restart + alert if failed 2+ consecutive times (avoid flaky single failures)
    if consecutive < 2:
        print(f"[{ts}] Waiting for next check to confirm...", file=sys.stderr)
        return

    # Check cooldown
    now_ts = time.time()
    if now_ts - last_restart < RESTART_COOLDOWN_S:
        remaining = int(RESTART_COOLDOWN_S - (now_ts - last_restart))
        print(f"[{ts}] Restart cooldown ({remaining}s left), skipping", file=sys.stderr)
        return

    # Restart
    print(f"[{ts}] Restarting {LAUNCHD_LABEL}...", file=sys.stderr)
    recovered = restart_service()

    state["last_restart_ts"] = now_ts
    state["consecutive_fails"] = 0 if recovered else consecutive
    save_state(state)

    # Alert
    status_icon = "\u2705" if recovered else "\u274c"
    alert_text = (
        f"\u26a0\ufe0f <b>AC Customer Support Service — Health Alert</b>\n\n"
        f"\u23f0 {ts}\n"
        f"\u274c Failed: {fail_summary}\n"
        f"\u267b\ufe0f Auto-restart: {status_icon} {'recovered' if recovered else 'STILL DOWN'}\n"
    )
    if not recovered:
        alert_text += "\n\u26a0\ufe0f Manual intervention needed!"

    send_telegram_alert(alert_text)
    print(f"[{ts}] Alert sent, recovered={recovered}", file=sys.stderr)


if __name__ == "__main__":
    main()
