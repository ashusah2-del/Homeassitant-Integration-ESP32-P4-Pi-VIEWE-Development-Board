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
    d.set_socketTimeout(3)
    d.set_sendWait(0.5)
    return d

def _make_gateway(gw_cfg):
    d = tinytuya.Device(
        dev_id=gw_cfg["id"],
        address=gw_cfg["ip"],
        local_key=gw_cfg["local_key"],
        version=float(gw_cfg.get("version", "3.4")),
    )
    d.set_socketTimeout(3)
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
    d.set_socketTimeout(3)
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
    if result is None:
        raise RuntimeError("No response from device (offline or unreachable)")
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


HOME_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tuya Local Bridge</title>
  <style>
    :root { color-scheme: dark; --bg:#071008; --card:#102016; --line:#27543a; --ok:#69f0ae; --warn:#ffb74d; --bad:#ff5252; --muted:#9fb0a6; --text:#f4fff8; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: radial-gradient(circle at top, #12301f, var(--bg)); color: var(--text); }
    header { position: sticky; top:0; z-index:2; background: rgba(7,16,8,.92); border-bottom:1px solid var(--line); padding:16px 20px; backdrop-filter: blur(8px); }
    h1 { margin:0; font-size: clamp(24px, 4vw, 36px); color: var(--ok); }
    header p { margin:6px 0 0; color: var(--muted); }
    main { max-width: 1180px; margin: 0 auto; padding: 18px; }
    .toolbar { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-bottom:16px; }
    button { border:0; border-radius:10px; padding:10px 14px; background:#1b5e20; color:white; font-weight:700; cursor:pointer; }
    button.secondary { background:#1e3a5f; }
    button.danger { background:#6a1b1a; }
    button:disabled { cursor:not-allowed; opacity:.45; }
    .pill { display:inline-flex; align-items:center; gap:8px; padding:8px 11px; border-radius:999px; border:1px solid var(--line); color:var(--muted); background:rgba(16,32,22,.75); }
    .dot { width:10px; height:10px; border-radius:50%; background:var(--warn); }
    .dot.ok { background:var(--ok); }
    .dot.bad { background:var(--bad); }
    .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap:16px; }
    .card { background:rgba(16,32,22,.93); border:1px solid var(--line); border-radius:16px; padding:16px; box-shadow:0 8px 22px rgba(0,0,0,.22); }
    .top { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
    h2 { margin:0; font-size:20px; }
    .meta { margin-top:4px; color:var(--muted); font-size:13px; overflow-wrap:anywhere; }
    .status { margin:14px 0; min-height:24px; font-size:16px; }
    .status.ok { color:var(--ok); }
    .status.warn { color:var(--warn); }
    .status.bad { color:var(--bad); }
    .controls { display:flex; gap:8px; flex-wrap:wrap; }
    .strip-row { display:grid; grid-template-columns: repeat(5, 1fr); gap:8px; }
    .strip-row button { padding:10px 4px; }
    pre { white-space:pre-wrap; overflow-wrap:anywhere; background:#061008; border:1px solid #1d3b2b; padding:10px; border-radius:10px; color:#d9ffe8; max-height:180px; overflow:auto; }
  </style>
</head>
<body>
  <header>
    <h1>Tuya Local Bridge</h1>
    <p>Local LAN dashboard for ESP32-P4 panel devices. No Tuya cloud calls are made here.</p>
  </header>
  <main>
    <div class="toolbar">
      <button onclick="loadDevices()">Refresh devices</button>
      <button class="secondary" onclick="refreshAll()">Refresh status</button>
      <span class="pill"><span id="health-dot" class="dot"></span><span id="health">Checking bridge...</span></span>
    </div>
    <section id="devices" class="grid"></section>
  </main>
  <script>
    const state = { devices: [] };

    function el(tag, attrs = {}, children = []) {
      const node = document.createElement(tag);
      for (const [key, value] of Object.entries(attrs)) {
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value);
      }
      for (const child of children) node.append(child);
      return node;
    }

    async function api(path, options = {}, timeoutMs = 6500) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      try {
        const res = await fetch(path, { ...options, signal: ctrl.signal });
        const data = await res.json().catch(() => ({}));
        return { httpOk: res.ok, status: res.status, data };
      } finally {
        clearTimeout(timer);
      }
    }

    function setHealth(ok, text) {
      document.getElementById("health-dot").className = `dot ${ok ? "ok" : "bad"}`;
      document.getElementById("health").textContent = text;
    }

    async function checkHealth() {
      try {
        const result = await api("/health", {}, 2500);
        setHealth(result.httpOk && result.data.ok, result.httpOk ? `Bridge online (${result.data.version || "unknown"})` : "Bridge error");
      } catch (err) {
        setHealth(false, "Bridge offline");
      }
    }

    async function loadDevices() {
      await checkHealth();
      const root = document.getElementById("devices");
      root.textContent = "Loading devices...";
      try {
        const result = await api("/devices", {}, 4000);
        if (!result.httpOk) throw new Error(result.data.error || `HTTP ${result.status}`);
        state.devices = result.data;
        root.textContent = "";
        for (const device of state.devices) root.append(renderDevice(device));
        refreshAll();
      } catch (err) {
        root.textContent = `Could not load devices: ${err.message}`;
      }
    }

    function renderDevice(device) {
      const card = el("article", { class: "card", id: `dev-${device.id}` });
      const title = el("div", { class: "top" }, [
        el("div", {}, [
          el("h2", { text: device.name || device.id }),
          el("div", { class: "meta", text: `${device.type || "device"} | ${device.ip ? `IP ${device.ip}` : `gateway ${device.gateway || "-"}`} | ${device.id}` }),
        ]),
        el("span", { class: "pill", text: device.configured ? "configured" : "missing key" }),
      ]);
      card.append(title, el("div", { class: "status warn", id: `status-${device.id}`, text: "Not refreshed yet" }));
      const controls = el("div", { class: device.type === "strip" ? "strip-row" : "controls", id: `controls-${device.id}` });
      card.append(controls);
      fillControls(device, controls);
      return card;
    }

    function fillControls(device, controls) {
      controls.textContent = "";
      if (!device.configured) {
        controls.append(el("span", { class: "status bad", text: "Local key not configured" }));
        return;
      }
      if (device.type === "gateway") {
        controls.append(el("button", { class: "secondary", text: "Refresh", onclick: () => refreshDevice(device) }));
      } else if (device.type === "strip") {
        for (const key of ["s1", "s2", "s3", "s4", "usb"]) {
          controls.append(el("button", { text: key.toUpperCase(), onclick: () => toggleStrip(device, key) }));
        }
      } else if (device.type === "lock") {
        controls.append(
          el("button", { text: "Lock", onclick: () => postAndRefresh(`/lock/${device.id}/lock`, device) }),
          el("button", { class: "danger", text: "Unlock", onclick: () => postAndRefresh(`/lock/${device.id}/unlock`, device) }),
        );
      } else if (device.type === "simple") {
        controls.append(
          el("button", { text: "On / Unlock", onclick: () => postAndRefresh(`/simple/${device.id}/on`, device) }),
          el("button", { class: "danger", text: "Off / Lock", onclick: () => postAndRefresh(`/simple/${device.id}/off`, device) }),
        );
      } else {
        controls.append(el("button", { class: "secondary", text: "Refresh", onclick: () => refreshDevice(device) }));
      }
    }

    function endpointFor(device) {
      if (device.type === "strip") return `/strip/${device.id}`;
      if (device.type === "lock") return `/lock/${device.id}`;
      if (device.type === "alarm") return `/alarm/${device.id}`;
      if (device.type === "sensor") return `/sensor/${device.id}`;
      if (device.type === "simple") return `/simple/${device.id}`;
      return null;
    }

    function setStatus(device, cssClass, text, data) {
      const node = document.getElementById(`status-${device.id}`);
      if (!node) return;
      node.className = `status ${cssClass}`;
      node.textContent = text;
      if (data && !data.ok && data.error) {
        const details = el("pre", { text: data.error });
        node.append(details);
      }
    }

    async function refreshDevice(device) {
      const endpoint = endpointFor(device);
      if (!endpoint) {
        setStatus(device, "warn", "Gateway entry - no direct state endpoint");
        return;
      }
      setStatus(device, "warn", "Refreshing...");
      try {
        const result = await api(endpoint);
        const data = result.data || {};
        if (!result.httpOk || !data.ok) {
          setStatus(device, "bad", "Offline / no response", data);
          return;
        }
        if (device.type === "strip") {
          setStatus(device, "ok", `S1 ${onOff(data.s1)} | S2 ${onOff(data.s2)} | S3 ${onOff(data.s3)} | S4 ${onOff(data.s4)} | USB ${onOff(data.usb)}`);
        } else if (device.type === "lock") {
          setStatus(device, data.locked ? "bad" : "ok", data.locked ? "Locked" : "Unlocked");
        } else if (device.type === "alarm") {
          setStatus(device, data.alarm ? "bad" : "ok", data.alarm ? "Alarm active" : "Clear");
        } else if (device.type === "sensor") {
          setStatus(device, data.state ? "bad" : "ok", data.state ? "Active" : "Clear");
        } else {
          setStatus(device, data.on ? "ok" : "warn", data.on ? "On / Unlocked" : "Off / Locked");
        }
      } catch (err) {
        setStatus(device, "bad", `Request failed: ${err.name === "AbortError" ? "timed out" : err.message}`);
      }
    }

    async function postAndRefresh(path, device) {
      setStatus(device, "warn", "Sending command...");
      try {
        const result = await api(path, { method: "POST" });
        if (!result.httpOk || !result.data.ok) {
          setStatus(device, "bad", result.data.message || result.data.error || `HTTP ${result.status}`, result.data);
          return;
        }
        await refreshDevice(device);
      } catch (err) {
        setStatus(device, "bad", `Command failed: ${err.name === "AbortError" ? "timed out" : err.message}`);
      }
    }

    async function toggleStrip(device, key) {
      const status = document.getElementById(`status-${device.id}`)?.textContent || "";
      const currentlyOn = new RegExp(`${key.toUpperCase()} ON`).test(status);
      await postAndRefresh(`/strip/${device.id}/${key}/${currentlyOn ? "off" : "on"}`, device);
    }

    async function refreshAll() {
      await checkHealth();
      for (const device of state.devices) refreshDevice(device);
    }

    function onOff(value) {
      return value ? "ON" : "OFF";
    }

    loadDevices();
  </script>
</body>
</html>
"""

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

    def _send_html(self, code, html):
        body = html.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            log.warning("client disconnected before HTML response was sent")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode() if length else ""

    def do_GET(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if not parts or parts == [""]:
            self._send_html(200, HOME_PAGE)
            return

        kind = parts[0]

        if kind == "health":
            self._send(200, {"ok": True, "version": "1.3"})

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
