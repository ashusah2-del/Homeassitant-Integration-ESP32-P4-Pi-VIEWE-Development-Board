#!/usr/bin/env python3
"""
Tuya Local Bridge — HTTP/REST server on port 8766.
Controls Tuya devices over LAN using tinytuya (no cloud subscription needed).
The ESP32-P4 panel calls this bridge directly to avoid HA's limited Tuya support.

Endpoints
---------
GET  /health                     → {"ok": true}
GET  /devices                    → list of all known devices with config state
GET  /simple/{id}                → {"ok": true/false, "on": bool}
POST /simple/{id}/on             → turn DPS 1 on
POST /simple/{id}/off            → turn DPS 1 off
GET  /strip/{id}                 → {"ok":bool,"s1":bool,"s2":bool,"s3":bool,"s4":bool,"usb":bool}
POST /strip/{id}/s{1-4}/on       → turn socket on
POST /strip/{id}/s{1-4}/off      → turn socket off
POST /strip/{id}/usb/on          → USB on
POST /strip/{id}/usb/off         → USB off
GET  /lock/{id}                  → {"ok": bool, "locked": bool}
POST /lock/{id}/lock             → lock the device
POST /lock/{id}/unlock           → unlock the device
GET  /alarm/{id}                 → {"ok": bool, "alarm": bool}
GET  /sensor/{id}                → {"ok": bool, "state": bool}
POST /keys                       → update local keys: [{"id":"...","key":"..."}]
"""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import tinytuya

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tuya-bridge")

PORT = 8766
DEVICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")


class TuyaBridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

# ── Config loading ────────────────────────────────────────────────────────────

def load_config():
    with open(DEVICES_FILE) as f:
        return json.load(f)

CONFIG = load_config()
_cfg_lock = threading.Lock()

def _find_device(device_id):
    """
    Returns (kind, dev_cfg, gw_cfg) where kind is 'wifi', 'gateway', or 'sub'.
    Returns None if not found.
    """
    with _cfg_lock:
        cfg = CONFIG
    for d in cfg.get("wifi_devices", []):
        if d["id"] == device_id:
            return ("wifi", d, None)
    for gw in cfg.get("gateways", []):
        if gw["id"] == device_id:
            return ("gateway", gw, None)
        for sub in gw.get("sub_devices", []):
            if sub["id"] == device_id:
                return ("sub", sub, gw)
    return None

# ── Device access (no persistent cache — tinytuya handles its own TCP) ────────

def _make_wifi_device(dev_cfg):
    d = tinytuya.OutletDevice(
        dev_id=dev_cfg["id"],
        address=dev_cfg["ip"],
        local_key=dev_cfg["local_key"],
        version=float(dev_cfg.get("version", "3.3")),
    )
    d.set_socketTimeout(6)
    d.set_sendWait(0.5)
    return d

def _make_gateway(gw_cfg):
    d = tinytuya.Device(
        dev_id=gw_cfg["id"],
        address=gw_cfg["ip"],
        local_key=gw_cfg["local_key"],
        version=float(gw_cfg.get("version", "3.4")),
    )
    d.set_socketTimeout(6)
    d.set_sendWait(0.5)
    return d

def _make_sub_device(sub_cfg, gw_cfg):
    gw = _make_gateway(gw_cfg)
    d = tinytuya.Device(
        dev_id=sub_cfg["id"],
        address=gw_cfg["ip"],
        local_key=gw_cfg["local_key"],
        version=float(gw_cfg.get("version", "3.4")),
        parent=gw,
    )
    d.set_socketTimeout(6)
    d.set_sendWait(0.5)
    return d

def _needs_key(cfg):
    return cfg.get("local_key", "REPLACE_ME") == "REPLACE_ME"

# ── Status fetchers ───────────────────────────────────────────────────────────

def _get_status(device_id):
    """Returns raw DPS dict or raises."""
    found = _find_device(device_id)
    if not found:
        raise ValueError(f"Unknown device: {device_id}")
    kind, dev_cfg, gw_cfg = found
    if kind == "wifi":
        if _needs_key(dev_cfg):
            raise RuntimeError("Local key not configured")
        d = _make_wifi_device(dev_cfg)
    elif kind == "gateway":
        if _needs_key(dev_cfg):
            raise RuntimeError("Local key not configured")
        d = _make_gateway(dev_cfg)
    else:
        if _needs_key(gw_cfg):
            raise RuntimeError("Gateway local key not configured")
        d = _make_sub_device(dev_cfg, gw_cfg)
    result = d.status()
    if "Error" in result:
        raise RuntimeError(result["Error"])
    return result.get("dps", {})

def _set_dps(device_id, dps_num, value):
    """Sets a single DPS value. Returns (ok, msg)."""
    found = _find_device(device_id)
    if not found:
        return False, f"Unknown device: {device_id}"
    kind, dev_cfg, gw_cfg = found
    try:
        if kind == "wifi":
            if _needs_key(dev_cfg):
                return False, "Local key not configured"
            d = _make_wifi_device(dev_cfg)
        elif kind == "gateway":
            if _needs_key(dev_cfg):
                return False, "Local key not configured"
            d = _make_gateway(dev_cfg)
        else:
            if _needs_key(gw_cfg):
                return False, "Gateway local key not configured"
            d = _make_sub_device(dev_cfg, gw_cfg)
        result = d.set_status(value, switch=dps_num)
        if isinstance(result, dict) and "Error" in result:
            return False, result["Error"]
        return True, "ok"
    except Exception as e:
        log.error("set_dps %s dps=%s val=%s: %s", device_id, dps_num, value, e)
        return False, str(e)

# ── Route handlers ────────────────────────────────────────────────────────────

def handle_simple_status(device_id):
    found = _find_device(device_id)
    if not found:
        return 404, {"ok": False, "error": "Device not found"}
    kind, dev_cfg, gw_cfg = found
    cfg = dev_cfg if kind != "sub" else dev_cfg
    gw = gw_cfg if kind == "sub" else None
    if _needs_key(gw or dev_cfg):
        return 503, {"ok": False, "on": False, "error": "Local key not configured"}
    try:
        dps = _get_status(device_id)
        dps_num = dev_cfg.get("dps_on", 1)
        on = bool(dps.get(str(dps_num), dps.get(dps_num, False)))
        return 200, {"ok": True, "on": on}
    except Exception as e:
        log.error("simple status %s: %s", device_id, e)
        return 503, {"ok": False, "on": False, "error": str(e)}

def handle_simple_set(device_id, state):
    found = _find_device(device_id)
    if not found:
        return 404, {"ok": False, "error": "Device not found"}
    _, dev_cfg, gw_cfg = found
    dps_num = dev_cfg.get("dps_on", 1)
    ok, msg = _set_dps(device_id, dps_num, state)
    return (200 if ok else 503), {"ok": ok, "message": msg}

def handle_strip_status(device_id):
    found = _find_device(device_id)
    if not found:
        return 404, {"ok": False, "error": "Device not found"}
    _, dev_cfg, gw_cfg = found
    if _needs_key(gw_cfg or dev_cfg):
        return 503, {"ok": False, "s1": False, "s2": False, "s3": False, "s4": False, "usb": False, "error": "Local key not configured"}
    try:
        dps = _get_status(device_id)
        dmap = dev_cfg.get("dps_map", {"s1": 1, "s2": 2, "s3": 3, "s4": 4, "usb": 7})
        result = {"ok": True}
        for k, v in dmap.items():
            result[k] = bool(dps.get(str(v), dps.get(v, False)))
        return 200, result
    except Exception as e:
        log.error("strip status %s: %s", device_id, e)
        return 503, {"ok": False, "s1": False, "s2": False, "s3": False, "s4": False, "usb": False, "error": str(e)}

def handle_strip_set(device_id, socket_key, state):
    found = _find_device(device_id)
    if not found:
        return 404, {"ok": False, "error": "Device not found"}
    _, dev_cfg, _ = found
    dmap = dev_cfg.get("dps_map", {"s1": 1, "s2": 2, "s3": 3, "s4": 4, "usb": 7})
    dps_num = dmap.get(socket_key)
    if dps_num is None:
        return 400, {"ok": False, "error": f"Unknown socket: {socket_key}"}
    ok, msg = _set_dps(device_id, dps_num, state)
    return (200 if ok else 503), {"ok": ok, "message": msg}

def handle_lock_status(device_id):
    found = _find_device(device_id)
    if not found:
        return 404, {"ok": False, "error": "Device not found"}
    _, dev_cfg, gw_cfg = found
    if _needs_key(gw_cfg or dev_cfg):
        return 503, {"ok": False, "locked": True, "error": "Local key not configured"}
    try:
        dps = _get_status(device_id)
        dps_num = dev_cfg.get("dps_locked", 8)
        # Most knob locks: dps_locked=True means locked
        locked = bool(dps.get(str(dps_num), dps.get(dps_num, True)))
        return 200, {"ok": True, "locked": locked}
    except Exception as e:
        log.error("lock status %s: %s", device_id, e)
        return 503, {"ok": False, "locked": True, "error": str(e)}

def handle_lock_set(device_id, action):
    found = _find_device(device_id)
    if not found:
        return 404, {"ok": False, "error": "Device not found"}
    _, dev_cfg, _ = found
    dps_num = dev_cfg.get("dps_locked", 8)
    # True = locked, False = unlocked
    state = (action == "lock")
    ok, msg = _set_dps(device_id, dps_num, state)
    return (200 if ok else 503), {"ok": ok, "message": msg}

def handle_alarm_status(device_id):
    found = _find_device(device_id)
    if not found:
        return 404, {"ok": False, "error": "Device not found"}
    _, dev_cfg, gw_cfg = found
    if _needs_key(gw_cfg or dev_cfg):
        return 503, {"ok": False, "alarm": False, "error": "Local key not configured"}
    try:
        dps = _get_status(device_id)
        dps_num = dev_cfg.get("dps_alarm", 1)
        alarm = bool(dps.get(str(dps_num), dps.get(dps_num, False)))
        return 200, {"ok": True, "alarm": alarm}
    except Exception as e:
        log.error("alarm status %s: %s", device_id, e)
        return 503, {"ok": False, "alarm": False, "error": str(e)}

def handle_sensor_status(device_id):
    found = _find_device(device_id)
    if not found:
        return 404, {"ok": False, "error": "Device not found"}
    _, dev_cfg, gw_cfg = found
    if _needs_key(gw_cfg or dev_cfg):
        return 503, {"ok": False, "state": False, "error": "Local key not configured"}
    try:
        dps = _get_status(device_id)
        dps_num = dev_cfg.get("dps_state", 1)
        state = bool(dps.get(str(dps_num), dps.get(dps_num, False)))
        return 200, {"ok": True, "state": state}
    except Exception as e:
        log.error("sensor status %s: %s", device_id, e)
        return 503, {"ok": False, "state": False, "error": str(e)}

def handle_list_devices():
    with _cfg_lock:
        cfg = CONFIG
    result = []
    for d in cfg.get("wifi_devices", []):
        result.append({
            "id": d["id"], "name": d["name"], "type": d.get("type", "simple"),
            "ip": d["ip"], "configured": not _needs_key(d),
        })
    for gw in cfg.get("gateways", []):
        result.append({
            "id": gw["id"], "name": gw["name"], "type": "gateway",
            "ip": gw["ip"], "configured": not _needs_key(gw),
        })
        for sub in gw.get("sub_devices", []):
            result.append({
                "id": sub["id"], "name": sub["name"], "type": sub.get("type", "simple"),
                "gateway": gw["id"], "configured": not _needs_key(gw),
            })
    return 200, result

def handle_update_keys(body):
    """Accept [{id: ..., key: ...}] and persist to devices.json."""
    global CONFIG
    try:
        updates = json.loads(body) if isinstance(body, (str, bytes)) else body
        if not isinstance(updates, list):
            return 400, {"ok": False, "error": "Expected a JSON array"}
    except Exception as e:
        return 400, {"ok": False, "error": str(e)}

    updated = []
    with _cfg_lock:
        for upd in updates:
            did = upd.get("id")
            key = upd.get("key")
            if not did or not key:
                continue
            found = False
            for d in CONFIG.get("wifi_devices", []):
                if d["id"] == did:
                    d["local_key"] = key
                    found = True
                    updated.append(did)
                    break
            if not found:
                for gw in CONFIG.get("gateways", []):
                    if gw["id"] == did:
                        gw["local_key"] = key
                        found = True
                        updated.append(did)
                        break
        with open(DEVICES_FILE, "w") as f:
            json.dump(CONFIG, f, indent=2)
    return 200, {"ok": True, "updated": updated}

# ── HTTP handler ──────────────────────────────────────────────────────────────

class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def _send(self, code, data):
        body = json.dumps(data).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.warning("client disconnected before response was sent")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode() if length else ""

    def do_GET(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if not parts or parts == [""]:
            self._send(200, {"ok": True, "service": "tuya-bridge"})
            return

        kind = parts[0]

        if kind == "health":
            self._send(200, {"ok": True, "version": "1.2"})

        elif kind == "devices":
            code, data = handle_list_devices()
            self._send(code, data)

        elif kind == "simple" and len(parts) == 2:
            code, data = handle_simple_status(parts[1])
            self._send(code, data)

        elif kind == "strip" and len(parts) == 2:
            code, data = handle_strip_status(parts[1])
            self._send(code, data)

        elif kind == "lock" and len(parts) == 2:
            code, data = handle_lock_status(parts[1])
            self._send(code, data)

        elif kind == "alarm" and len(parts) == 2:
            code, data = handle_alarm_status(parts[1])
            self._send(code, data)

        elif kind == "sensor" and len(parts) == 2:
            code, data = handle_sensor_status(parts[1])
            self._send(code, data)

        else:
            self._send(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        kind = parts[0] if parts else ""

        # POST /keys  — bulk key update
        if kind == "keys" and len(parts) == 1:
            body = self._read_body()
            code, data = handle_update_keys(body)
            self._send(code, data)
            return

        # POST /simple/{id}/on|off
        if kind == "simple" and len(parts) == 3:
            device_id = parts[1]
            action = parts[2]
            if action in ("on", "off"):
                code, data = handle_simple_set(device_id, action == "on")
                self._send(code, data)
            else:
                self._send(400, {"ok": False, "error": f"Unknown action: {action}"})
            return

        # POST /strip/{id}/s1..s4|usb/on|off
        if kind == "strip" and len(parts) == 4:
            device_id = parts[1]
            socket_key = parts[2]
            action = parts[3]
            if action in ("on", "off"):
                code, data = handle_strip_set(device_id, socket_key, action == "on")
                self._send(code, data)
            else:
                self._send(400, {"ok": False, "error": f"Unknown action: {action}"})
            return

        # POST /lock/{id}/lock|unlock
        if kind == "lock" and len(parts) == 3:
            device_id = parts[1]
            action = parts[2]
            if action in ("lock", "unlock"):
                code, data = handle_lock_set(device_id, action)
                self._send(code, data)
            else:
                self._send(400, {"ok": False, "error": f"Unknown action: {action}"})
            return

        self._send(404, {"ok": False, "error": "Not found"})


if __name__ == "__main__":
    log.info("Tuya local bridge starting on port %d", PORT)
    log.info("Devices file: %s", DEVICES_FILE)
    server = TuyaBridgeServer(("0.0.0.0", PORT), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
