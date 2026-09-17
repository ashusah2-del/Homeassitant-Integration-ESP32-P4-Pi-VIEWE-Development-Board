#!/usr/bin/env python3
"""HA freeze catcher — runs on the Docker host (.59), survives a Pi freeze.

Every INTERVAL seconds it hits HA's /api/config and records latency + core
state to health.log. When HA is slow or unresponsive it captures core logs +
resources over SSH into a timestamped capture-*.txt while HA is still (barely)
reachable, so the event-loop-blocking warning that names the culprit is saved
even if the Pi later has to be power-cycled.

Env (ha-freeze-watch.env): HA_URL, HA_TOKEN, HA_SSH (user@host), SSH_PORT.
"""
import os, time, json, subprocess, datetime, urllib.request

HA_URL = os.environ.get("HA_URL", "http://192.168.55.4:8123").rstrip("/")
TOKEN = os.environ["HA_TOKEN"].strip().strip('"')
SSH = os.environ.get("HA_SSH", "root@192.168.55.4")
SSH_PORT = os.environ.get("SSH_PORT", "22222")
INTERVAL = int(os.environ.get("INTERVAL", "30"))
SLOW_MS = int(os.environ.get("SLOW_MS", "6000"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "8"))
DIR = os.path.dirname(os.path.abspath(__file__))
HEALTH = os.path.join(DIR, "health.log")

_last_capture = 0.0


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
    log(f"{now()}  --- freeze-watch started (interval={INTERVAL}s slow>{SLOW_MS}ms) ---")
    while True:
        ms, state = probe()
        if ms is None:
            log(f"{now()}  UNREACHABLE  state={state}")
            capture(f"unreachable:{state}")
        elif ms > SLOW_MS or state != "RUNNING":
            log(f"{now()}  SLOW  {ms}ms  state={state}")
            capture(f"slow:{ms}ms/{state}")
        else:
            log(f"{now()}  ok  {ms}ms  state={state}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
