#!/usr/bin/env python3
"""HA freeze catcher — runs on the Docker host (.59), survives a Pi freeze.

Every INTERVAL seconds it hits HA's /api/config and records latency + core
state to health.log. When HA is slow or unresponsive it captures core logs +
resources over SSH into a timestamped capture-*.txt while HA is still (barely)
reachable, so the event-loop-blocking warning that names the culprit is saved
even if the Pi later has to be power-cycled.

Env (ha-freeze-watch.env): HA_URL, HA_TOKEN, HA_SSH (user@host), SSH_PORT.
"""
import os, time, json, subprocess, datetime, urllib.request, urllib.parse

HA_URL = os.environ.get("HA_URL", "http://192.168.55.4:8123").rstrip("/")
TOKEN = os.environ["HA_TOKEN"].strip().strip('"')
SSH = os.environ.get("HA_SSH", "root@192.168.55.4")
SSH_PORT = os.environ.get("SSH_PORT", "22222")
INTERVAL = int(os.environ.get("INTERVAL", "30"))
SLOW_MS = int(os.environ.get("SLOW_MS", "6000"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "8"))
FAIL_THRESHOLD = int(os.environ.get("FAIL_THRESHOLD", "2"))  # consecutive misses before alert
AUTO_RESTART = os.environ.get("AUTO_RESTART", "1") not in ("0", "", "false", "no")
RESTART_AFTER = int(os.environ.get("RESTART_AFTER", "300"))  # seconds unreachable before ha core restart
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT", "").strip()
DIR = os.path.dirname(os.path.abspath(__file__))
HEALTH = os.path.join(DIR, "health.log")

_last_capture = 0.0


def ha_core_restart():
    """Graceful `ha core restart` over SSH (SSH add-on is a separate container,
    usually alive even when core is frozen). Never `docker restart` — that breaks
    the supervisor. Returns (ok, output_tail)."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             "-p", SSH_PORT, SSH, "ha core restart"],
            capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[-300:]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def notify(text):
    """Out-of-band Telegram alert (works while HA is down). No-op if unconfigured."""
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=15)
    except Exception as e:
        log(f"{now()}  telegram notify failed: {e}")


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def log(line):
    with open(HEALTH, "a") as f:
        f.write(line + "\n")


def probe():
    """Return (latency_ms, state) or (None, 'TIMEOUT'/'ERR:..')."""
    req = urllib.request.Request(f"{HA_URL}/api/config",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            state = json.load(r).get("state", "?")
        return int((time.monotonic() - t) * 1000), state
    except Exception as e:
        return None, f"{type(e).__name__}"


def capture(reason):
    global _last_capture
    if time.monotonic() - _last_capture < 120:  # rate-limit captures
        return
    _last_capture = time.monotonic()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(DIR, f"capture-{ts}.txt")
    cmd = ("echo '### date'; date; "
           "echo '### free'; free -h; "
           "echo '### uptime'; uptime; "
           "echo '### top'; top -bn1 2>/dev/null | head -20; "
           "echo '### core logs (last 600)'; ha core logs --lines 600 2>&1")
    try:
        out = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             "-p", SSH_PORT, SSH, cmd],
            capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        out = f"CAPTURE SSH FAILED: {e}"
    with open(path, "w") as f:
        f.write(f"# capture reason: {reason} @ {now()}\n\n{out}")
    log(f"{now()}  *** CAPTURE written: {os.path.basename(path)} ({reason}) ***")


def main():
    log(f"{now()}  --- freeze-watch started (interval={INTERVAL}s slow>{SLOW_MS}ms "
        f"telegram={'on' if TG_TOKEN and TG_CHAT else 'off'} "
        f"auto_restart={'on' if AUTO_RESTART else 'off'}@{RESTART_AFTER}s) ---")
    misses = 0            # consecutive unreachable polls
    alerted = False       # have we sent a DOWN alert for the current outage?
    restarted = False     # have we issued ha core restart for this outage?
    down_since = None
    while True:
        ms, state = probe()
        if ms is None:
            misses += 1
            log(f"{now()}  UNREACHABLE  state={state}  (miss {misses})")
            capture(f"unreachable:{state}")
            if down_since is None:
                down_since = datetime.datetime.now()
            down_secs = (datetime.datetime.now() - down_since).total_seconds()
            if not alerted and misses >= FAIL_THRESHOLD:
                alerted = True
                notify(f"🔴 Home Assistant UNREACHABLE from Docker host (.59)\n"
                       f"since ~{down_since.strftime('%H:%M:%S')} ({state}). "
                       f"Logs captured on .59 (ha-freeze-watch/capture-*.txt).")
            # auto-heal: one graceful `ha core restart` per outage after RESTART_AFTER
            if AUTO_RESTART and not restarted and down_secs >= RESTART_AFTER:
                restarted = True
                capture("pre-restart")  # keep the pre-restart logs for diagnosis
                log(f"{now()}  >>> auto ha core restart (down {int(down_secs)}s)")
                notify(f"⏳ HA unreachable ~{int(down_secs)//60}m — issuing "
                       f"`ha core restart` from .59…")
                ok, out = ha_core_restart()
                log(f"{now()}  restart {'OK' if ok else 'FAILED'}: {out}")
                notify(("↻ ha core restart issued." if ok else
                        "⚠️ ha core restart FAILED (SSH/core wedged?) — may need a "
                        "manual power-cycle.\n") + (f"\n{out}" if out else ""))
        else:
            if alerted:  # recovering from an outage we alerted on
                dur = int((datetime.datetime.now() - down_since).total_seconds())
                how = " (after auto-restart)" if restarted else ""
                notify(f"🟢 Home Assistant reachable again after "
                       f"{dur//3600}h{dur%3600//60}m{dur%60}s{how} ({ms}ms, state={state}).")
            misses = 0
            alerted = False
            restarted = False
            down_since = None
            if ms > SLOW_MS or state != "RUNNING":
                log(f"{now()}  SLOW  {ms}ms  state={state}")
                capture(f"slow:{ms}ms/{state}")
            else:
                log(f"{now()}  ok  {ms}ms  state={state}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
