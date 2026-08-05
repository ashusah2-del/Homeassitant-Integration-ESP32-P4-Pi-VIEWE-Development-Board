# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A custom Home Assistant control panel running on a **Guition ESP32-P4 + ESP32-C6 7-inch development board** (1024×600, 32 MB PSRAM, 16 MB flash). The active project lives entirely under `projects/home-assistant-p4-c6-panel/`. The repo root also contains board vendor reference examples and datasheets; ignore those unless you're doing low-level driver work.

## Active project: `projects/home-assistant-p4-c6-panel/`

### Subsystems

| Path | Role |
|---|---|
| `esphome/mangalam-panel.yaml` | **Production entry point.** All substitutions (entity IDs, cast targets, proxy URLs) live here. |
| `esphome/guition_p4_7inch_compat.yaml` | Board-level node config (pin assignments, PSRAM, ESP32C6 hosted-mode SDIO, `esp32-p4-evboard`). |
| `esphome/packages/core_ha.yaml` | ESPHome API (noise-encrypted, port 6053), OTA, safe_mode, HA sensors. |
| `esphome/packages/display_touch_board.yaml` | All LVGL UI pages, online_image, swipe navigation, idle timer. |
| `esphome/packages/theme_palettes.yaml` | 5 runtime colour palettes (Midnight/Graphite/Warm Home/Nord/High Contrast) + `apply_panel_theme`. |
| `esphome/packages/audio_voice_board.yaml` | I2S codec, microWakeWord ("Okay Nabu"), voice assistant pipeline. |
| `esphome/packages/tuya_local.yaml` | Tuya LAN page + HTTP scripts. |
| `esphome/packages/jellyfin_local.yaml` | Jellyfin browse page (posters, pagination, cast target selection). |
| `home_assistant/packages/esp32p4_panel.yaml` | HA-side YAML: input_text/input_number helpers, automations (calendar refresh, Jellyfin play script). Drop into HA packages folder. |
| `jellyfin-proxy/proxy.py` | Python service (port 8767). Exposes `/health`, `/movies`, `/poster/<id>`, `POST /play/<id>` for the panel. |
| `hypon-proxy/proxy.py` | Python service (port 8769). Logs into Hypontech Cloud directly and polls all 5 API endpoints (HA's own integration only uses 2) — exposes grid power, home load, battery SOC, inverter/gateway status, CO2/trees, earnings via `/hypon/status` for HA's `rest:` sensors in `esp32p4_panel.yaml`. |
| `tuya-bridge/bridge.py` | Tuya LAN bridge (port 8766) using tinytuya. Devices in `devices.json`. |

### Page navigation

```
slideshow ─tap─▶ dashboard ◀─swipe─▶ tado ◀─swipe─▶ presence ◀─swipe─▶ energy ◀─swipe─▶ cameras ◀─swipe─▶ tuya ◀─swipe─▶ jellyfin
   ▲                                                                                                                           │
   └───────────────────────────── 30 s idle timeout ──────────────────────────────────────────────────────────────────────────┘
```

## Build + flash

Builds run inside the ESPHome dashboard Docker container (`esphome`, compose file at `~/docker/esphome/docker-compose.yml`). **Its `/config` is a real bind mount to `~/docker/esphome/config/` — a separate, unsynced copy of this repo's `esphome/` tree, NOT a symlink.** Editing files in this repo has no effect on what gets compiled until you manually `cp` the changed file(s) across first. Forgetting this step silently compiles/flashes stale code with no warning. Sync before every compile:

```bash
cp esphome/mangalam-panel.yaml esphome/mangalam-panel-2.yaml ~/docker/esphome/config/
cp esphome/packages/*.yaml ~/docker/esphome/config/packages/
```

```bash
# Compile
docker exec esphome esphome compile /config/mangalam-panel.yaml

# OTA upload
docker exec esphome esphome upload /config/mangalam-panel.yaml --device <panel-ip>
```

Fresh builds: 10–12 min (TFLite-micro). Incremental with warm ccache: 25–70 s.

## Deploy HA package

Copy `home_assistant/packages/esp32p4_panel.yaml` to the HA packages folder on the Pi 5 and reload HA config. SSH access: `ssh root@<ha-ip>` (hassio shell).

## Proxy services (Docker host)

Config files (gitignored in prod):

```bash
# Jellyfin proxy
cp jellyfin-proxy/proxy.env.example jellyfin-proxy/proxy.env
# Fill: JELLYFIN_URL, JELLYFIN_API_KEY

# Tuya bridge
cp tuya-bridge/devices.json.example tuya-bridge/devices.json
# Fill device IPs, local keys, device IDs
```

All three run as systemd user services (`.service` files in each subdirectory).

## Known gotchas — read before editing

- **`ESPTime::day_of_week` is 1..7 (Sunday=1), not 0..6.** Indexing a 7-element array OOB on Saturdays corrupts memory and triggers ESP-IDF auto-rollback. Always `wd[day_of_week - 1]` with a bounds clamp.
- **JPEGDEC crashes on SOF1 and full-size SOF0 under PSRAM XIP.** Always pass images through the proxy — never send raw Immich previews or full-size camera frames directly to the panel.
- **LVGL 9 API names:** `lv_img_set_zoom` / `lv_img_set_pivot` are dead shims. Use `lv_image_set_scale` / `lv_image_set_pivot`. Centre images with `lv_image_set_inner_align(LV_IMAGE_ALIGN_CENTER)`.
- **Font glyphs are subset at compile time.** Characters not literally present in the YAML are dropped. Avoid `−` (U+2212), `●`, `←`, `→` — use ASCII equivalents or load a full-font copy.
- **API `max_connections`** is set to 6 in `core_ha.yaml` (default is 5), deliberately kept low. The ESPHome dashboard "Logs" tab leaks `esphome logs` subprocesses that hold API slots. If the panel stops responding to HA pushes after an OTA, run `docker restart esphome`.
- **Tuya page:** don't fire parallel HTTP poll requests. All six device polls are sequential with a single UI update at the end; `g_tuya_busy` blocks taps during in-flight requests. The same busy-flag pattern (`g_jf_busy`, `g_ih_busy`) is used for Jellyfin and iHost polling too.
- **safe_mode rollback:** `core_ha.yaml` sets `num_attempts: 5` with `boot_is_good_after: 60s` — ESPHome auto-rolls back to the previous image after 5 consecutive failed boots. After a crash loop, the panel may be running stale firmware — check the version on the Settings page before debugging the current code.

## Secrets

`esphome/packages/secrets.yaml` is gitignored. Regenerate from `esphome/secrets.yaml.example`:

```bash
cp esphome/secrets.yaml.example esphome/packages/secrets.yaml
openssl rand -base64 32   # api_encryption_key
openssl rand -base64 24   # ota_password
```

## Committing

Use `scripts/save_change.sh "commit message"` — it stages everything, commits, and pushes.
