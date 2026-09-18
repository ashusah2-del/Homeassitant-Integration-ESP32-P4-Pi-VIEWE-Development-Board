"""AppDaemon app: Hypontech Cloud -> HA sensors, no proxy, no REST sensor.

Ports the old hypon-proxy polling into AppDaemon. Because AppDaemon is a
separate process, the blocking cloud login/poll can't stall HA's event loop.
Writes the same 19 sensor.hypon_* entities the REST platform used to create,
so dashboards/automations keep working unchanged.

apps.yaml:
  hypon:
    module: hypon
    class: Hypon
    username: "..."
    password: "..."
    poll_interval: 60
"""
import json
import time
import urllib.error
import urllib.request

import appdaemon.plugins.hass.hassapi as hass

BASE_URL = "https://api.hypon.cloud/v2"
TOKEN_LIFETIME_SEC = 3000  # tokens valid ~3600s; re-login before that


class Hypon(hass.Hass):
    def initialize(self):
        self.username = self.args["username"]
        self.password = self.args["password"]
        self.poll_interval = int(self.args.get("poll_interval", 60))
        self._token = None
        self._token_expires_at = 0.0
        # first poll shortly after start, then every poll_interval seconds
        self.run_in(self.poll, 3)
        self.run_every(self.poll, "now+%d" % self.poll_interval, self.poll_interval)
        self.log("Hypon app initialized (poll every %ss)" % self.poll_interval)

    # ---- Hypontech API ----
    def _login(self):
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token
        req = urllib.request.Request(
            BASE_URL + "/login",
            data=json.dumps({"username": self.username, "password": self.password}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        self._token = result["data"]["token"]
        self._token_expires_at = now + TOKEN_LIFETIME_SEC
        return self._token

    def _get(self, path):
        token = self._login()
        req = urllib.request.Request(BASE_URL + path, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._token = None  # force re-login next call
            raise

    def _get_paginated(self, path_fmt):
        items, page, total_pages = [], 1, 1
        while page <= total_pages:
            sep = "&" if "?" in path_fmt else "?"
            result = self._get("%s%spage=%d" % (path_fmt, sep, page))
            total_pages = result.get("totalPage", 1)
            items.extend(result.get("data", []))
            page += 1
        return items

    def _poll_once(self):
        overview = self._get("/plant/overview")["data"]
        plants = self._get_paginated("/plant/list2?page_size=10&refresh=true")
        earning = (overview.get("earning") or [{}])[0]

        flat = {
            "overview": {
                "capacity_kw": overview.get("capacity"),
                "devices_normal": overview.get("normal_dev_num"),
                "devices_fault": overview.get("fault_dev_num"),
                "devices_offline": overview.get("offline_dev_num"),
                "total_co2_kg": overview.get("total_co2"),
                "total_trees": overview.get("total_tree"),
                "earning_currency": earning.get("currency"),
                "earning_today": earning.get("today"),
                "earning_total": earning.get("total"),
            },
            "plant": None,
        }
        if plants:
            plant = plants[0]
            plant_id = plant["plant_id"]
            monitor = self._get("/plant/%s/monitor?refresh=true" % plant_id)["data"]
            inverters = self._get_paginated("/plant/%s/inverter" % plant_id)
            inv0 = inverters[0] if inverters else {}
            flat["plant"] = {
                "grid_power_w": monitor.get("meter_power"),
                "home_load_w": monitor.get("power_load"),
                "pv_power_w": monitor.get("power_pv"),
                "battery_power_w": monitor.get("w_cha"),
                "battery_soc_pct": monitor.get("soc"),
                "e_month_kwh": monitor.get("e_month"),
                "e_year_kwh": monitor.get("e_year"),
                "inverter_status": inv0.get("status"),
                "gateway_status": (inv0.get("gateway") or {}).get("status"),
                "fault_count": sum(1 for i in inverters if i.get("fault")),
                "warning_count": sum(1 for i in inverters if i.get("warning")),
            }
        return flat

    # ---- publish to HA ----
    def _pub(self, eid, state, name, unit=None, device_class=None, state_class=None, icon=None):
        if state is None:
            return
        state = str(state)  # HA /api/states expects string states (int 0 etc. 400s)
        attrs = {"friendly_name": name}
        if unit:
            attrs["unit_of_measurement"] = unit
        if device_class:
            attrs["device_class"] = device_class
        if state_class:
            attrs["state_class"] = state_class
        if icon:
            attrs["icon"] = icon
        self.set_state(eid, state=state, attributes=attrs)

    def poll(self, kwargs=None):
        try:
            f = self._poll_once()
        except Exception as e:
            self.log("poll failed, keeping last values: %s" % e, level="WARNING")
            return
        o = f["overview"]
        cur = o.get("earning_currency")
        self._pub("sensor.hypon_capacity", o.get("capacity_kw"), "Hypon Capacity", "kW", icon="mdi:solar-power-variant")
        self._pub("sensor.hypon_devices_normal", o.get("devices_normal"), "Hypon Devices Normal", icon="mdi:check-circle")
        self._pub("sensor.hypon_devices_fault", o.get("devices_fault"), "Hypon Devices Fault", icon="mdi:alert-circle")
        self._pub("sensor.hypon_devices_offline", o.get("devices_offline"), "Hypon Devices Offline", icon="mdi:lan-disconnect")
        self._pub("sensor.hypon_total_co2_saved", o.get("total_co2_kg"), "Hypon Total CO2 Saved", "kg", icon="mdi:molecule-co2")
        self._pub("sensor.hypon_total_trees_equivalent", o.get("total_trees"), "Hypon Total Trees Equivalent", icon="mdi:tree")
        self._pub("sensor.hypon_earning_today", o.get("earning_today"), "Hypon Earning Today", cur, icon="mdi:cash")
        self._pub("sensor.hypon_earning_total", o.get("earning_total"), "Hypon Earning Total", cur, icon="mdi:cash-multiple")

        p = f["plant"]
        if p:
            self._pub("sensor.hypon_grid_power", p.get("grid_power_w"), "Hypon Grid Power", "W", "power", "measurement")
            self._pub("sensor.hypon_home_load", p.get("home_load_w"), "Hypon Home Load", "W", "power", "measurement")
            self._pub("sensor.hypon_pv_power", p.get("pv_power_w"), "Hypon PV Power", "W", "power", "measurement")
            self._pub("sensor.hypon_battery_power", p.get("battery_power_w"), "Hypon Battery Power", "W", "power", "measurement")
            self._pub("sensor.hypon_battery_soc", p.get("battery_soc_pct"), "Hypon Battery SOC", "%", "battery", "measurement")
            self._pub("sensor.hypon_energy_this_month", p.get("e_month_kwh"), "Hypon Energy This Month", "kWh", "energy", "total_increasing")
            self._pub("sensor.hypon_energy_this_year", p.get("e_year_kwh"), "Hypon Energy This Year", "kWh", "energy", "total_increasing")
            self._pub("sensor.hypon_inverter_status", p.get("inverter_status"), "Hypon Inverter Status", icon="mdi:current-ac")
            self._pub("sensor.hypon_inverter_fault_count", p.get("fault_count"), "Hypon Inverter Fault Count", icon="mdi:alert")
            self._pub("sensor.hypon_inverter_warning_count", p.get("warning_count"), "Hypon Inverter Warning Count", icon="mdi:alert-outline")
            self._pub("sensor.hypon_gateway_status", p.get("gateway_status"), "Hypon Gateway Status", icon="mdi:wifi")
        self.log("poll ok (pv=%sW soc=%s%%)" % (p.get("pv_power_w") if p else "?", p.get("battery_soc_pct") if p else "?"))
