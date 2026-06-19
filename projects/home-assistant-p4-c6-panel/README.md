# Mangalam Smart-Home Panel

A custom **Home Assistant control panel** built on the Guition
**ESP32-P4 + ESP32-C6 7-inch development board** (1024×600 IPS
capacitive touch, 32 MB PSRAM, 16 MB flash).

This is a personal-use panel for a wall-mounted whole-house dashboard,
running **ESPHome 2026.5+ on LVGL 9** and integrating with a
**Home Assistant OS** instance on a Raspberry Pi 5. It replaces a
generic tablet showing a Lovelace dashboard with a purpose-built
UI driven directly by HA's native API.

## Feature pages

- **Slideshow / lock screen** with random Immich photos, a left-side
  calendar overlay, and a wake-word status pill
- **Room dashboard** — 6 rooms with light bulb icons, on/off, brightness
  sliders, and per-room temperature + humidity
- **Tado climate** — 6 thermostat zones with current-temperature
  readout, ⊖/⊕ target buttons, and OFF/AUTO/HEAT mode buttons
- **Presence** — 12 motion/occupancy sensors (indoor + outdoor) with
  recoloured `mdi:home` indicators
- **Energy** — Home Assistant energy flow diagram for grid import,
  solar production, live load, and export readings
- **Cameras** — 2×2 grid of Eufy thumbnails, tap any to open the
  larger live view (manual Refresh button, no auto-polling)
- **Tuya local** — direct LAN control of strips, locks, plugs, and
  safety sensors via the tuya-bridge service
- **Settings** — live status, backlight, slideshow/opacity/transition tunables

Navigation between pages is a left/right swipe gesture; the slideshow
re-engages after 30 seconds of inactivity:

```
slideshow ─tap─▶ dashboard ◀─swipe─▶ tado ◀─swipe─▶ presence ◀─swipe─▶ energy ◀─swipe─▶ cameras ◀─swipe─▶ tuya
```

## Repository layout

| Path | Contents |
|---|---|
| `esphome/` | **The actual panel firmware.** Start here. |
| `esphome/README.md` | Detailed feature documentation, HA entity mappings, gotchas |
| `esphome/guition_p4_7inch_compat.yaml` | Top-level node config |
| `esphome/packages/` | Modular package fragments (`core_ha`, `display_touch_board`, `audio_voice_board`) |
| `firmware/` | (Legacy) ESP-IDF starter app — not used in production |
| `home_assistant/` | HA YAML helpers that go on the HA OS side |
| `mechanical/housing/` | OpenSCAD housing model |

## Supporting services

The panel is backed by a small **Python proxy service** running on the
Docker host (`docker/immich-proxy/`, not in this repo). It exposes two
endpoints used by the panel:

- `GET /random-photo` — re-encodes a random Immich image as a
  guaranteed SOF0 baseline JPEG resized for the panel. It uses the full
  image library by default; set `FAVORITES_ONLY=true` in `proxy.env` to
  restrict the slideshow to favourites, or set `IMMICH_PERSON_NAMES`
  to only show photos containing at least one configured person.
- `GET /camera/<entity>?size=thumb|full` — re-encodes a Eufy camera
  snapshot at a panel-friendly size

The Tuya LAN bridge runs separately on port `8766`. Open
`http://<docker-host-ip>:8766/` for a local dashboard that lists
configured Tuya devices, refreshes their state, and sends the same
control requests used by the ESP32-P4 panel.

## Hardware

- Board: VIEWE / Guition 7-inch ESP32-P4 development board
  (`esp32-p4-evboard`)
- Display: 1024 × 600 MIPI DSI, 16-bit colour
- Touch: GT911 capacitive
- Audio: I2S codec with built-in mic + speaker
- Co-processor: ESP32-C6 (Wi-Fi 6 / BT5 / Thread) — used for Wi-Fi,
  talks to the P4 over hosted-mode SDIO
- Power: USB-C, with battery monitoring on GPIO53

## Quick start

See **[esphome/README.md](./esphome/README.md)** for the detailed
build, flash, HA-wiring walkthrough, and a list of known gotchas
collected the hard way during development (font-glyph subsetting,
LVGL 9 image binding, JPEGDEC limits, ESPHome dashboard zombie
connections, …).

## License

Personal-use config; reuse / fork at your own risk. The hardware
reference photos and reference manual at the repo root belong to the
upstream board vendor.
