#!/usr/bin/env python3
"""
iHost Open API v2 proxy for ESP32-P4 panel.
Port 8768.  Talks to the eWeLink iHost local REST API using a Bearer token.

Token setup: press the physical button on the iHost, then run get_token.sh
within the 5-minute window.  The token persists until the iHost is factory-reset.

Endpoints
---------
GET  /health                     → {"ok": true, "devices": N}
GET  /devices                    → [{"serial":"…","name":"…","on":bool|null}]
GET  /device/<serial>            → {"ok":bool,"serial":"…","name":"…","on":bool|null}
POST /device/<serial>/on         → {"ok":bool}
POST /device/<serial>/off        → {"ok":bool}
POST /device/<serial>/toggle     → {"ok":bool,"on":bool|null}
POST /device/<serial>            → body {"on":true/false}  (HA rest switch compat)
"""

import json
import logging
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ihost-proxy")

IHOST_URL = os.environ.get("IHOST_URL", "http://ihost.local").rstrip("/")
IHOST_TOKEN = os.environ["IHOST_TOKEN"]
PORT = int(os.environ.get("PROXY_PORT", "8768"))
API_BASE = f"{IHOST_URL}/open-api/v2/rest"

_TIMEOUT = 8


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {IHOST_TOKEN}",
        "Content-Type": "application/json",
    }


def _get_json(path: str) -> dict:
    req = urllib.request.Request(f"{API_BASE}{path}", headers=_headers())
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _put_json(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=data, headers=_headers(), method="PUT"
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _parse_device(d: dict) -> dict:
    serial = d.get("serialNumber", "")
    name = d.get("name", serial)
    power = d.get("state", {}).get("power", {})
    on = (power.get("powerState") == "on") if power else None
    return {"serial": serial, "name": name, "on": on}


def _device_list() -> list:
    resp = _get_json("/devices")
    return [_parse_device(d) for d in resp.get("data", {}).get("deviceList", [])]


def _device_state(serial: str) -> dict | None:
    try:
        resp = _get_json("/devices")
        for d in resp.get("data", {}).get("deviceList", []):
            if d.get("serialNumber") == serial:
                parsed = _parse_device(d)
                return {"ok": True, **parsed}
        return None
    except Exception:
        return None


def _set_power(serial: str, on: bool) -> bool:
    try:
        resp = _put_json(
            f"/devices/{serial}/state",
            {"power": {"powerState": "on" if on else "off"}},
        )
        return resp.get("error", 1) == 0
    except Exception:
        return False


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def _send(self, code: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/health":
            try:
                devices = _device_list()
                self._send(200, {"ok": True, "devices": len(devices)})
            except Exception as exc:
                log.warning("health check failed: %s", exc)
                self._send(503, {"ok": False, "error": str(exc)})

        elif path == "/devices":
            try:
                self._send(200, _device_list())
            except Exception as exc:
                log.warning("devices list failed: %s", exc)
                self._send(503, {"ok": False, "error": str(exc)})

        elif path.startswith("/device/"):
            serial = path[len("/device/"):]
            if not serial:
                self._send(400, {"ok": False, "error": "serial required"})
                return
            state = _device_state(serial)
            if state is None:
                self._send(404, {"ok": False, "on": None})
            else:
                self._send(200, state)

        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        parts = [p for p in path.split("/") if p]

        # POST /device/<serial>/on|off|toggle
        if len(parts) == 3 and parts[0] == "device" and parts[2] in ("on", "off", "toggle"):
            serial = parts[1]
            action = parts[2]

            if action == "toggle":
                state = _device_state(serial)
                if state is None:
                    self._send(404, {"ok": False})
                    return
                target = not bool(state.get("on"))
            else:
                target = (action == "on")

            ok = _set_power(serial, target)
            self._send(200 if ok else 502, {"ok": ok, "on": target if ok else None})

        # POST /device/<serial>  with body {"on": bool}  — for HA rest switch
        elif len(parts) == 2 and parts[0] == "device":
            serial = parts[1]
            try:
                payload = json.loads(self._read_body().decode())
                target = bool(payload.get("on", False))
            except Exception:
                self._send(400, {"ok": False, "error": "bad JSON body"})
                return
            ok = _set_power(serial, target)
            self._send(200 if ok else 502, {"ok": ok, "on": target if ok else None})

        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    log.info("iHost proxy starting on port %d → %s", PORT, API_BASE)
    server = _ThreadingHTTPServer(("", PORT), Handler)
    server.serve_forever()
