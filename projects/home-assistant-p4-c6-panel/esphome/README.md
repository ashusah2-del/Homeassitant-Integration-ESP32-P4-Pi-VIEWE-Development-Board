# Mangalam Smart-Home Panel — ESPHome Firmware

A custom 1024×600 touch panel firmware for the **Guition ESP32-P4 + ESP32-C6
7-inch development board** (`esp32-p4-evboard`), built on ESPHome
2026.5+ / LVGL 9 and integrated with **Home Assistant** running on a
Raspberry Pi 5.

The panel replaces a wall-mounted HA dashboard tablet. It boots to a
photo-frame style **Immich slideshow** when idle, then exposes
**five purpose-built pages of live home controls** by swipe gesture:

```
slideshow ─tap─▶ dashboard ◀─swipe─▶ tado ◀─swipe─▶ presence ◀─swipe─▶ cameras
   ▲                                                                        │
   └────── 30s idle timeout ────────────────────────────────────────────────┘
```

---

## Feature pages

### 1. Slideshow / lock screen (`slideshow_page`)

The default idle screen, modelled on a digital photo frame.

- **Random Immich photos** every 10 minutes, fetched via a small Python
  proxy on the Docker host. The proxy re-encodes every preview as a
  guaranteed SOF0 baseline JPEG resized to ≤ 512×400, which works
  around the JPEGDEC 1.2.7 crash on SOF1 (extended-sequential)
  thumbnails and on full-resolution previews under ESP32-P4 PSRAM XIP.
- **Calendar panel** (left 340 px) shows today's weekday/date plus up
  to eight upcoming events pushed from HA into
  `input_text.panel_calendar_events`. Row 0 falls back to
  "No events this week" if the entity hasn't been published yet.
- **Stock fallback images** (2 × 1024×600 RGB565 embedded in flash) are
  rotated through if Immich is unreachable after three retries.
- **HA-disconnected overlay** appears full-screen if no API client is
  connected, with a "reconnecting…" hint.
- Tap anywhere to advance to the dashboard.

### 2. Room dashboard (`dashboard_page`)

3×2 grid of room cards driven directly from HA entities. Each card
shows a `mdi:lightbulb` icon (gold when on, grey when off), the room
name, an on/off label, a 0–100 % brightness slider for dimmable
fixtures, and ambient temperature + humidity where sensors exist.

| Card | Light | Brightness | Temperature | Humidity |
|---|---|---|---|---|
| Drawing Room | `switch.drawinglights` | — | `sensor.drawing_room_weather_temperature` | `sensor.drawing_room_weather_humidity` |
| Office | `light.office_lights` | ✓ | `sensor.office_room_temperature` | `sensor.office_room_humidity` |
| Hallway | `light.hallway` | ✓ | — | — |
| Stairs | `light.stairs_light` | ✓ | `sensor.stairs_weather_temperature` | `sensor.stairs_weather_humidity` |
| Bedroom | `light.bedroom_lights` | ✓ | `sensor.h5075_5b8d_temperature` | `sensor.h5075_5b8d_humidity` |
| Conservatory | `switch.conservatory_switch` | — | — | — |

Tap the body of a card to toggle the light/switch (`homeassistant.toggle`).
Drag the slider to set brightness (`light.turn_on` with `brightness_pct`).

### 3. Tado climate (`tado_page`)

3×2 grid of climate zone cards for the household's Tado thermostats.
Swipe right from this page returns to the dashboard; swipe left moves
to the presence page.

Each card shows the **current temperature** + **humidity**, a centred
**target temperature** with ⊖ / ⊕ buttons that adjust in 0.5 °C steps,
and an **OFF / AUTO / HEAT** row that fires
`climate.set_hvac_mode`. The mode label in the top-right of every card
is colour-coded (blue = AUTO, orange = HEAT, grey = OFF).

Zones: Drawing Room, Office, Main Bedroom, Mukta, Advik, and the
whole-house Heating valve (`climate.heating`, styled with an orange
accent to distinguish from individual rooms).

### 4. Presence (`presence_page`)

4×3 grid (12 cards) covering every motion / occupancy sensor in the
house and the outdoor Eufy-camera motion zones. Each card shows the
sensor name, a recoloured `mdi:home` indicator (green = ACTIVE,
grey = CLEAR) and an ACTIVE/CLEAR label.

| Card | HA entity |
|---|---|
| Hallway | `binary_sensor.hallway_motion_sensor_motion` |
| Hallway PS | `binary_sensor.hallway_ps_motion` |
| Stairs | `binary_sensor.stairs_motion_sensor_motion` |
| Drawing | `binary_sensor.dr_motion_sensor_motion_2` |
| Office | `binary_sensor.office_presence_sensor_occupancy` |
| Toilet | `binary_sensor.tze200_3towulqd_ts0601_motion_4` |
| Conservatory | `binary_sensor.cps_motion` |
| Repeater | `binary_sensor.repeater_motion` |
| Front Door | `binary_sensor.front_door_motion_detected` |
| Front Bell | `binary_sensor.front_door_bell_motion_detected` |
| Side Door | `binary_sensor.side_door_motion_detected` |
| Garden | `binary_sensor.garden_motion_detected` |

### 5. Cameras (`camera_page` + `camera_view_page`)

2×2 grid of 4 Eufy outdoor cameras. Thumbnails refresh from the proxy
every 60 seconds; tapping a tile opens `camera_view_page` with the
larger live frame (up to 600×340). The "Back" button or a
swipe-right returns to the grid. Live view refreshes only when
the user re-taps a tile (the earlier 3-second polling interval was
removed after it caused the panel to crash with
"Instruction address misaligned" during the 800×500 decode).

| Card | HA entity |
|---|---|
| Front Door | `camera.front_door` |
| Front Bell | `camera.front_door_bell` |
| Side Door | `camera.side_door` |
| Garden | `camera.garden` |

### 6. Settings (`settings_page`)

Read-only status: ESPHome version, IP, RSSI, HA connection state, and
the active proxy URL. Reached from the "Settings" link in the
dashboard's bottom bar.

---

## Supporting services

### Immich proxy (`docker/immich-proxy/`)

A small `BaseHTTPRequestHandler` Python service running as a systemd
**user** service (`immich-proxy.service`) on the Docker host
(`<docker-host-ip>:8765`). It exposes two endpoints used by the panel:

- `GET /random-photo` — fetches a random IMAGE asset from Immich,
  follows the `?size=preview` thumbnail link, then **always**
  re-encodes via Pillow as SOF0 baseline JPEG resized to
  ≤ `MAX_W × MAX_H` (default 512 × 400). Removes both the SOF1
  incompatibility and the full-size decode-memory pressure.
- `GET /camera/<entity>?size=thumb|full` — fetches an HA
  `/api/camera_proxy/camera.<entity>` snapshot using a long-lived
  HA token, then resizes (300×200 for thumbs, 800×500 for full) and
  re-encodes via Pillow. Same SOF0 guarantee.

Config lives in `proxy.env` (gitignored): Immich URL + API key, HA URL
+ long-lived token, JPEG quality, retry count.

### Home-Assistant integration

The panel uses ESPHome's native API (port 6053, noise-encrypted) with
`max_connections: 12` (raised from the default 5 because stale
`esphome logs` subprocesses from the dashboard add-on tend to
accumulate and exhaust the slot pool, locking out HA).

It exposes the standard ESPHome-managed entities (`Panel Online`,
`Panel Backlight`, `Panel Uptime`, RSSI, IP, SSID, battery voltage,
speaker enable) and subscribes to **all the HA entities listed in the
tables above** plus `input_text.panel_calendar_events` and
`input_text.panel_immich_url` for runtime overrides.

---

## Repository layout

```
home-assistant-p4-c6-panel/esphome/
├── README.md                           ← you are here
├── guition_p4_7inch_compat.yaml        ← board-level entry node
├── packages/
│   ├── core_ha.yaml                    ← API, OTA, safe_mode, HA sensors
│   ├── display_touch_board.yaml        ← LVGL UI, all pages, online_image, scripts
│   ├── audio_voice_board.yaml          ← I2S codec, microWakeWord, voice assistant
│   └── secrets.yaml                    ← (gitignored)
└── secrets.yaml.example
```

The live ESPHome dashboard symlinks
`/home/mangalam/docker/esphome/config/mangalam-panel.yaml` to this
config. Builds run inside the dashboard container; OTAs use the fast
`esphome.espota2.run_ota` Python loop in `tools/fast_ota.py` (not in
this repo — see CLAUDE.md memory for the snippet).

---

## Build + flash

Generate the secrets file once (long-lived API key and OTA password):

```bash
cp secrets.yaml.example packages/secrets.yaml
openssl rand -base64 32   # → api_encryption_key
openssl rand -base64 24   # → ota_password
```

Then from the ESPHome dashboard or CLI:

```bash
esphome compile guition_p4_7inch_compat.yaml
esphome upload guition_p4_7inch_compat.yaml --device <panel-ip>
```

Fresh builds take 10–12 minutes (TFLite-micro is the slow stage);
incremental rebuilds with ccache warm are 25–70 seconds.

---

## Known gotchas / footguns

A handful of issues that bit us hard while bringing this up — kept
visible so the next builder doesn't rediscover them.

- **`ESPTime::day_of_week` is 1..7 (Sunday=1), not 0..6.** Indexing a
  7-element weekday array with the raw value reads OOB on Saturdays,
  corrupts memory, and triggers an ESP-IDF auto-rollback on the next
  boot. Always do `wd[day_of_week - 1]` with a bounds clamp.

- **JPEGDEC 1.2.7 crashes on SOF1 JPEGs and on full-size SOF0 previews
  under PSRAM XIP.** Don't try to "preserve quality" by passing
  full-size images to the panel. Always re-encode through the proxy.

- **LVGL 9 renamed the image API**: `lv_img_set_zoom` is a no-op shim
  on this build — use `lv_image_set_scale`. `lv_img_set_pivot` →
  `lv_image_set_pivot`. `lv_image_set_inner_align(LV_IMAGE_ALIGN_CENTER)`
  is the cleanest way to centre an image inside a fixed-size widget.

- **Fonts are subset by ESPHome at compile time.** Any character that
  doesn't appear literally in your YAML gets dropped from the
  Montserrat builds and renders as a missing-glyph rectangle. We hit
  this with `−` (U+2212), `●` (U+25CF), `←`, and `→`. Stick to ASCII
  in labels or load full-font copies if you need typography.

- **API max_connections defaults to 5 on ESP32-P4 builds.** The
  ESPHome dashboard container's "Logs" tab spawns `esphome logs`
  subprocesses that don't die when the browser disconnects — they
  pile up, hold an API slot each, and eventually lock HA out. Bump
  `api.max_connections` and `docker restart esphome` if the panel
  goes "deaf" to HA pushes after an OTA.

- **Don't poll camera live view aggressively.** A 3-second polling
  loop on an 800×500 RGB565 image (≈ 720 KB working set per frame)
  fragments PSRAM fast enough to corrupt the LVGL display buffer
  ("Instruction address misaligned" on the next render). Refresh on
  user gesture, not on a timer.

- **`button.text:` accepts only `format:`/`args:`.** Font and colour
  go *outside* the `text:` block, as siblings at the button level —
  `text_font:` and `text_color:` inside `text:` is a YAML validation
  error.

---

## License

This config is provided as-is under the same license as the parent
repository. The hardware reference photos, calendar entries, room
labels and HA entity IDs are obviously personal to my setup — fork
and re-wire the substitutions in `guition_p4_7inch_compat.yaml`
to match yours.
