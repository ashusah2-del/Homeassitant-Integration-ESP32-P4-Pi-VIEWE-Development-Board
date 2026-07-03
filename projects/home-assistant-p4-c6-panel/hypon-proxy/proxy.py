#!/usr/bin/env python3
"""
Hypontech Cloud proxy for the ESP32-P4 panel / Home Assistant.

HA's built-in Hypontech Cloud integration only surfaces 3 fields
(power, today energy, lifetime energy) from 2 of the API's 5 endpoints.
This proxy logs into the same account directly and polls all of them,
exposing every field the cloud actually returns (grid power, home load,
battery SOC, inverter faults, CO2/trees, earnings, month/year energy...)
as flat JSON that HA's `rest:` sensor platform can scrape.

Endpoints:
  GET /health        -> 200 OK once at least one poll has succeeded
  GET /hypon/status   -> full flattened JSON snapshot (cached, refreshed
                          every HYPON_POLL_SEC seconds by a background thread)
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hypon-proxy")

BASE_URL = "https://api.hypon.cloud/v2"
HYPON_USERNAME = os.environ["HYPON_USERNAME"]
HYPON_PASSWORD = os.environ["HYPON_PASSWORD"]
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8769"))
POLL_SEC = int(os.environ.get("HYPON_POLL_SEC", "60"))
TOKEN_LIFETIME_SEC = 3000  # API tokens are valid ~3600s; re-login before that

_lock = threading.Lock()
_state: dict = {}
_state_ready = threading.Event()
_token: str | None = None
_token_expires_at: float = 0.0


def _login() -> str:
    global _token, _token_expires_at
    now = time.time()
    if _token and now < _token_expires_at:
        return _token
    req = urllib.request.Request(
        f"{BASE_URL}/login",
        data=json.dumps({"username": HYPON_USERNAME, "password": HYPON_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    _token = result["data"]["token"]
    _token_expires_at = now + TOKEN_LIFETIME_SEC
    return _token


def _get(path: str) -> dict:
    token = _login()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Token rejected server-side — force a fresh login on next call.
            global _token
            _token = None
            raise
        raise


def _get_paginated(path_fmt: str) -> list:
    items: list = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        sep = "&" if "?" in path_fmt else "?"
        result = _get(f"{path_fmt}{sep}page={page}")
        total_pages = result.get("totalPage", 1)
        items.extend(result.get("data", []))
        page += 1
    return items


def _poll_once() -> dict:
    overview = _get("/plant/overview")["data"]
    plants = _get_paginated("/plant/list2?page_size=10&refresh=true")

    earning = (overview.get("earning") or [{}])[0]

    flat: dict = {
        "overview": {
            "capacity_kw": overview.get("capacity"),
            "power_w": overview.get("power") if overview.get("company", "W").upper() == "W" else overview.get("power", 0) * 1000,
            "percent": overview.get("percent"),
            "e_today_kwh": overview.get("e_today"),
            "e_total_kwh": overview.get("e_total"),
            "devices_normal": overview.get("normal_dev_num"),
            "devices_fault": overview.get("fault_dev_num"),
            "devices_offline": overview.get("offline_dev_num"),
            "devices_waiting": overview.get("wait_dev_num"),
            "total_co2_kg": overview.get("total_co2"),
            "total_trees": overview.get("total_tree"),
            "earning_currency": earning.get("currency"),
            "earning_today": earning.get("today"),
            "earning_total": earning.get("total"),
        },
        "plants": {},
        "last_updated": int(time.time()),
    }

    for plant in plants:
        plant_id = plant["plant_id"]
        monitor = _get(f"/plant/{plant_id}/monitor?refresh=true")["data"]
        inverters = _get_paginated(f"/plant/{plant_id}/inverter")

        plant_entry = {
            "name": plant.get("plant_name"),
            "type": plant.get("plant_type"),
            "status": plant.get("status"),
            "city": plant.get("city"),
            "country": plant.get("country"),
            "power_w": plant.get("power") if plant.get("company", "W").upper() == "W" else plant.get("power", 0) * 1000,
            "e_today_kwh": plant.get("e_today"),
            "e_total_kwh": plant.get("e_total"),
            "monitor": {
                "grid_power_w": monitor.get("meter_power"),
                "home_load_w": monitor.get("power_load"),
                "pv_power_w": monitor.get("power_pv"),
                "battery_power_w": monitor.get("w_cha"),
                "battery_soc_pct": monitor.get("soc"),
                "capacity_percent": monitor.get("percent"),
                "e_today_kwh": monitor.get("e_today"),
                "e_month_kwh": monitor.get("e_month"),
                "e_year_kwh": monitor.get("e_year"),
                "e_total_kwh": monitor.get("e_total"),
                "earning_currency": monitor.get("monetary"),
                "earning_today": monitor.get("today_earning"),
                "earning_month": monitor.get("month_earning"),
                "earning_total": monitor.get("total_earning"),
                "total_co2_kg": monitor.get("total_co2"),
                "total_trees": monitor.get("total_tree"),
                "total_diesel_l": monitor.get("total_diesel"),
                "warning": monitor.get("warning"),
            },
            "inverters": [],
        }

        for inv in inverters:
            gateway = inv.get("gateway") or {}
            battery = inv.get("battery") or {}
            plant_entry["inverters"].append({
                "sn": inv.get("sn"),
                "model": inv.get("model"),
                "software_version": inv.get("software_version"),
                "status": inv.get("status"),
                "fault": inv.get("fault"),
                "warning": inv.get("warning"),
                "power_w": inv.get("power"),
                "e_today_kwh": inv.get("e_today"),
                "e_total_kwh": inv.get("e_total"),
                "gateway_sn": gateway.get("sn"),
                "gateway_model": gateway.get("model"),
                "gateway_status": gateway.get("status"),
                "battery_status": battery.get("status"),
                "battery_soc_pct": battery.get("soc"),
                "battery_capacity_wh": battery.get("wh"),
                "battery_voltage_v": battery.get("v_bat"),
                "battery_current_a": battery.get("a_bat"),
            })

        plant_entry["fault_count"] = sum(1 for i in inverters if i.get("fault"))
        plant_entry["warning_count"] = sum(1 for i in inverters if i.get("warning"))

        flat["plants"][plant_id] = plant_entry

    return flat


def _poll_loop() -> None:
    while True:
        try:
            new_state = _poll_once()
            with _lock:
                global _state
                _state = new_state
            _state_ready.set()
            log.info(
                "poll ok: overview power=%sW, %d plant(s)",
                new_state["overview"].get("power_w"),
                len(new_state["plants"]),
            )
        except Exception:
            log.exception("poll failed, keeping last known state")
        time.sleep(POLL_SEC)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200 if _state_ready.is_set() else 503, {"ok": _state_ready.is_set()})
            return
        if self.path == "/hypon/status":
            if not _state_ready.is_set():
                self._json(503, {"error": "no data yet"})
                return
            with _lock:
                self._json(200, _state)
            return
        self._json(404, {"error": "not found"})


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main() -> None:
    threading.Thread(target=_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), Handler)
    log.info("Hypon Cloud proxy starting on :%d (poll every %ds)", PROXY_PORT, POLL_SEC)
    server.serve_forever()


if __name__ == "__main__":
    main()
