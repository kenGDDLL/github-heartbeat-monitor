#!/usr/bin/env python3
"""
Polls a Telegram bot's chat for "TG <id> is alive" heartbeat messages,
updates state.json + docs/index.html, and emails an alert if any device
newly transitions from up/unknown to down.

Required environment variables (set as GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID            e.g. -1003859236761
Optional:
  THRESHOLD_MINUTES           default 270 (4h30m)
  HEARTBEAT_INTERVAL_MINUTES  default 240 (4h)
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS
  ALERT_EMAIL_FROM, ALERT_EMAIL_TO
  (if SMTP_HOST is unset, email sending is skipped and a note is printed)
"""
import json
import os
import re
import sys
import smtplib
import ssl
from email.mime.text import MIMEText
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.parse import urlencode

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO_ROOT, "state.json")
DASHBOARD_PATH = os.path.join(REPO_ROOT, "docs", "index.html")

DEVICE_IDS = ["DC", "ISI", "OPS", "NET", "Master", "RPS"]
DEVICE_LABELS = {d: "TG " + d for d in DEVICE_IDS}
HEARTBEAT_PATTERN = re.compile(r"TG\s+(\S+)\s+is\s+alive", re.IGNORECASE)


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {
            "devices": {d: {"label": DEVICE_LABELS[d], "lastSeenIso": None} for d in DEVICE_IDS},
            "lastUpdateId": 0,
            "lastPollIso": None,
            "lastPollOk": False,
            "lastPollNote": "No sync has run yet.",
        }
    with open(STATE_PATH) as f:
        state = json.load(f)
    # backfill any missing devices/fields defensively
    state.setdefault("devices", {})
    for d in DEVICE_IDS:
        state["devices"].setdefault(d, {"label": DEVICE_LABELS[d], "lastSeenIso": None})
    state.setdefault("lastUpdateId", 0)
    return state


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def telegram_api(method, token, **params):
    url = "https://api.telegram.org/bot{}/{}".format(token, method)
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "heartbeat-monitor/1.0"})
    with urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError("Telegram API error: {}".format(payload))
    return payload["result"]


def status_of(last_seen_iso, threshold_minutes, now):
    if not last_seen_iso:
        return "unknown"
    elapsed = (now - parse_iso(last_seen_iso)).total_seconds() / 60.0
    return "up" if elapsed <= threshold_minutes else "down"


def render_dashboard(state):
    with open(DASHBOARD_PATH) as f:
        html = f.read()

    payload = {
        "devices": state["devices"],
        "heartbeatIntervalMinutes": state.get("heartbeatIntervalMinutes", 240),
        "thresholdMinutes": state.get("thresholdMinutes", 270),
        "lastPollIso": state.get("lastPollIso"),
        "lastPollOk": state.get("lastPollOk", False),
        "lastPollNote": state.get("lastPollNote", ""),
    }
    new_block = (
        '<script type="application/json" id="device-state">\n'
        + json.dumps(payload, indent=2)
        + "\n</script>"
    )
    updated = re.sub(
        r'<script type="application/json" id="device-state">.*?</script>',
        new_block.replace("\\", "\\\\"),
        html,
        flags=re.S,
    )
    with open(DASHBOARD_PATH, "w") as f:
        f.write(updated)


def send_alert(newly_down, state):
    smtp_host = os.environ.get("SMTP_HOST")
    to_addr = os.environ.get("ALERT_EMAIL_TO")
    if not smtp_host or not to_addr:
        print("SMTP_HOST/ALERT_EMAIL_TO not configured — skipping email, alert was:")
        for d in newly_down:
            print("  ALERT: {} is DOWN".format(state["devices"][d]["label"]))
        return

    lines = []
    for d in newly_down:
        last_seen = state["devices"][d].get("lastSeenIso") or "never"
        lines.append("- {} — last heartbeat: {}".format(state["devices"][d]["label"], last_seen))
    body = "The following device(s) have missed their expected heartbeat window:\n\n" + "\n".join(lines)
    subject = "ALERT: {} device(s) DOWN — {}".format(
        len(newly_down), ", ".join(state["devices"][d]["label"] for d in newly_down)
    )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.environ.get("ALERT_EMAIL_FROM", os.environ.get("SMTP_USER", ""))
    msg["To"] = to_addr

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, port, timeout=20) as server:
        server.starttls(context=context)
        if user and password:
            server.login(user, password)
        server.sendmail(msg["From"], [to_addr], msg.as_string())
    print("Alert email sent to {}".format(to_addr))


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        sys.exit(1)
    chat_id = int(chat_id)

    threshold_minutes = int(os.environ.get("THRESHOLD_MINUTES", "270"))
    heartbeat_minutes = int(os.environ.get("HEARTBEAT_INTERVAL_MINUTES", "240"))

    state = load_state()
    state["thresholdMinutes"] = threshold_minutes
    state["heartbeatIntervalMinutes"] = heartbeat_minutes

    now = now_utc()
    # "before" status must reflect what we believed at the END of the previous run
    # (persisted as lastKnownStatus), NOT a fresh recompute against "now" — a device
    # can go from up to down purely because time passed with no new heartbeat, and
    # recomputing "before" with today's "now" would erase that transition (before
    # and after would always match if no message arrived). Fall back to a fresh
    # computation only on the very first run when lastKnownStatus doesn't exist yet.
    before_status = {
        d: state["devices"][d].get("lastKnownStatus")
        or status_of(state["devices"][d].get("lastSeenIso"), threshold_minutes, now)
        for d in DEVICE_IDS
    }

    try:
        offset = state.get("lastUpdateId", 0) + 1
        max_update_id = state.get("lastUpdateId", 0)
        any_new = False
        while True:
            results = telegram_api("getUpdates", token, offset=offset, limit=100, timeout=0)
            if not results:
                break
            for item in results:
                max_update_id = max(max_update_id, item["update_id"])
                msg = (item.get("message") or item.get("edited_message") or item.get("channel_post") or item.get("edited_channel_post"))
                if not msg:
                    continue
                if msg.get("chat", {}).get("id") != chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                m = HEARTBEAT_PATTERN.search(text)
                if not m:
                    continue
                raw_id = m.group(1)
                canonical = next((d for d in DEVICE_IDS if d.lower() == raw_id.lower()), None)
                if not canonical:
                    continue
                msg_iso = iso(datetime.fromtimestamp(msg["date"], tz=timezone.utc))
                existing = state["devices"][canonical].get("lastSeenIso")
                if not existing or msg_iso > existing:
                    state["devices"][canonical]["lastSeenIso"] = msg_iso
                any_new = True
            offset = max_update_id + 1
            if len(results) < 100:
                break

        state["lastUpdateId"] = max_update_id
        state["lastPollIso"] = iso(now)
        state["lastPollOk"] = True
        state["lastPollNote"] = "Processed new heartbeat(s)." if any_new else "No new heartbeats this cycle."
    except Exception as exc:
        # Telegram was unreachable this cycle. We still can't learn about new
        # heartbeats, but a device timing out is purely a function of elapsed
        # time, so we deliberately fall through to the down-detection below
        # instead of returning early — a real outage shouldn't go unnoticed
        # just because this one poll attempt failed.
        state["lastPollIso"] = iso(now)
        state["lastPollOk"] = False
        state["lastPollNote"] = "Poll failed: {}".format(exc)
        print("Poll failed: {}".format(exc), file=sys.stderr)

    after_status = {
        d: status_of(state["devices"][d].get("lastSeenIso"), threshold_minutes, now) for d in DEVICE_IDS
    }
    newly_down = [d for d in DEVICE_IDS if before_status[d] in ("up", "unknown") and after_status[d] == "down"]

    # Persist this run's computed status so the NEXT run can detect a pure
    # timeout transition (no message needed) by comparing against it.
    for d in DEVICE_IDS:
        state["devices"][d]["lastKnownStatus"] = after_status[d]

    save_state(state)
    render_dashboard(state)

    if newly_down:
        send_alert(newly_down, state)
    else:
        print("Sync ok — no status changes.")


if __name__ == "__main__":
    main()
