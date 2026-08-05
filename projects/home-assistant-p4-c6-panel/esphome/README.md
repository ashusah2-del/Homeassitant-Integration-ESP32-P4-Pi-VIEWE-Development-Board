# Mangalam Smart-Home Panel — ESPHome Firmware

A custom 1024×600 touch panel firmware for the **Guition ESP32-P4 + ESP32-C6
7-inch development board** (`esp32-p4-evboard`), built on ESPHome
2026.5+ / LVGL 9 and integrated with **Home Assistant** running on a
Raspberry Pi 5.

The panel replaces a wall-mounted HA dashboard tablet. It boots to a
photo-frame style **Immich slideshow** when idle, then exposes
**eight purpose-built pages of live home controls** by swipe gesture:

```
slideshow ─tap─▶ dashboard ◀─swipe─▶ tado ◀─swipe─▶ presence ◀─swipe─▶ energy ◀─swipe─▶ cameras ◀─swipe─▶ tuya ◀─swipe─▶ jellyfin
   ▲                                                                                                                          │
   └────────────────────────────── 30s idle timeout ───────────────────────────────────────────────────────────────────────────┘
```

---

## Feature pages

### 1. Slideshow / lock screen (`slideshow_page`)

The default idle screen, modelled on a digital photo frame.

- **Random Immich photos** every 10 minutes, fetched via a small Python
  proxy on the Docker host. The proxy re-encodes every preview as a
  guaranteed SOF0 baseline JPEG on a fixed 800×480, 16-pixel-aligned
  canvas, which works around JPEGDEC 1.2.7 crashes/corruption on SOF1
  and odd-sized portrait thumbnails under ESP32-P4 PSRAM XIP.
- **Calendar panel** (left 340 px) shows today's weekday/date plus up
  to eight upcoming events pushed from HA into
  `input_text.panel_calendar_events`. Row 0 falls back to
  "No events this week" if the entity hasn't been published yet.
- **Stock fallback images** (2 × 1024×600 RGB565 embedded in flash) are
  rotated through if Immich is unreachable after three retries.
- **HA-disconnected overlay** appears full-screen if no API client is
  connected, with a "reconnecting…" hint.
- **Wake-word status pill** shows `Wake: Ready` with a pulsing dot
  while local "Okay Nabu" detection is armed (colours follow the active
  panel theme), then changes through Listening / Thinking / Speaking /
  Error states during Assist.
- **Layout settings** are launched from Settings. They let the calendar,
  weather widget, clock/date, and wake-word pill be shown or hidden, then
  dragged around the slideshow; visibility and positions are saved on the
  panel and restored after reboot.
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

### 5. Energy (`energy_page`)

A Home Assistant-driven energy diagram between Presence and Cameras.
It shows grid power, solar production, home load, and battery SOC —
grid/solar/home/battery all come from the Hypon Cloud proxy (see
`hypon-proxy/`), since the account is a hybrid solar+battery system.
Daily import/export billing totals are separate, from Octopus. The
HA entities are configured via substitutions in `mangalam-panel.yaml`:

- `energy_grid_power`
- `energy_solar_power`
- `energy_solar_energy` (today's kWh subtitle)
- `energy_home_power`
- `energy_battery_power`
- `energy_battery_soc`
- `energy_today_import`
- `energy_today_export`

### 6. Cameras (`camera_page` + `camera_view_page`)

2×2 grid of 4 Eufy outdoor cameras. Thumbnails refresh from the proxy
every 60 seconds; tapping a tile opens `camera_view_page` with the
larger live frame (up to 600×340). The "Back" button, a swipe-right,
or the **Refresh** button controls the live view. Live frames refresh
on open and when Refresh is tapped (the earlier 3-second polling
interval was removed after it caused the panel to crash with
"Instruction address misaligned" during the 800×500 decode).

| Card | HA entity |
|---|---|
| Front Door | `camera.front_door` |
| Front Bell | `camera.front_door_bell` |
| Side Door | `camera.side_door` |
| Garden | `camera.garden` |

### 7. Tuya local (`tuya_page`)

Direct LAN control of Tuya devices via the `tuya-bridge` Python service
(port 8766). Power strips, locks, WiFi plugs, smoke/CO/water sensors,
and alarm — no Home Assistant round-trip required. Swipe right from
Cameras, left to return to the slideshow.

### 8. Settings (`settings_page`)

Live status (HA connection, Immich, IP, RSSI, battery, uptime, firmware
version, Tuya bridge) plus on-panel tunables:

- Slideshow interval (1–1440 min, syncs to `input_number.panel_slideshow_interval`)
- Calendar overlay opacity (0–100 %, syncs to `input_number.panel_calendar_opacity`)
- Slide transition effect (6 modes, syncs to `input_number.panel_transition_effect`)
- **Panel colour theme** — five palettes cycled with `<` / `>`; syncs
  bidirectionally to HA helper `input_select.panel_theme`
- Backlight brightness (10–100 %)
- Layout Settings subpage for showing/hiding and positioning the
  calendar, weather, time, and wake-word overlays

Reached from the "Settings" link in the dashboard's bottom bar.
Immich URL and calendar events can still be edited via HA helpers.

---

## Panel colour themes

Five switchable palettes unify the entire LVGL UI at runtime via semantic
tokens in `packages/theme_palettes.yaml`:

| Index | Name | Character |
|---|---|---|
| 0 | Midnight | Green accent, current production look |
| 1 | Graphite | Neutral Material dark, blue accent |
| 2 | Warm Home | Amber/cozy tones |
| 3 | Nord | Frost palette |
| 4 | High Contrast | Accessibility (yellow on black) |

**On-panel:** Settings → scroll the left column → **Panel colour theme**
→ `<` / `>`.

**Home Assistant:** create a Dropdown helper so changes sync both ways:

```
Settings → Devices & Services → Helpers → Create Helper → Dropdown
  Name:       Panel Theme
  Entity ID:  input_select.panel_theme
  Options:    Midnight
              Graphite
              Warm Home
              Nord
              High Contrast
```

When the panel cycles a theme it calls `input_select.select_option`; when
you change the helper in HA the panel applies the matching palette on the
next state push. The chosen index is also stored in NVS (`g_panel_theme`)
so the panel remembers its theme across reboots.

Themed surfaces include page backgrounds, header/footer bars, cards
(dashboard, Tado, presence, energy, cameras, Tuya), slideshow overlays,
voice/wake pills, crash/HA-disconnect banners, and runtime status colours
(lamps, presence dots, Tuya device states).

### Design tokens (compile-time defaults)

All YAML styling pulls from substitution tokens defined at the top of
`mangalam-panel.yaml` instead of scattered hex literals:

- **Layout metrics** — `screen_w/screen_h`, `header_h`, `footer_h`,
  `content_h`, `card_radius`, `btn_radius`, `nav_btn_w/h`. Headers and
  footers are `width: 100%`; the card grids on the dashboard, climate,
  presence, and camera pages are LVGL flex containers with
  percentage-sized cards, so the layout re-flows from the display's
  actual resolution rather than assuming 1024×600. C++ lambdas
  (slideshow transitions, drag bounds, layout defaults) read
  `lv_disp_get_hor_res()` at runtime for the same reason.
- **Colour tokens** — `c_page_bg`, `c_card_bg`, `c_accent`, `c_error`,
  etc. These are the *boot defaults* (Midnight); `apply_panel_theme`
  repaints every themed surface a couple of seconds after boot and on
  every theme change.

### Status toast + central error handling

A single toast on the LVGL top layer (`error_bar`/`error_lbl`) is the
error surface for the whole panel — it floats over any page, auto-hides
after 8 s, and dismisses on tap. Integrations report failures through
the parameterized `panel_toast` script; bridge health polls
(Tuya/Jellyfin/iHost) only toast on an online→offline *transition* so a
dead service doesn't nag every poll. The slideshow's download retry
logic is shared between both double-buffer slots via
`slideshow_loaded`/`slideshow_failed(slot)`, and camera thumbnails
refresh through one `refresh_cam_slot(slot)` script. The crash banner,
HA-offline pill, and voice-assistant pill also live on the top layer,
so they're visible regardless of the active page.

---

## Supporting services

### Immich + camera images

The standalone `immich-proxy` service (formerly `:8765`) has been removed.
Its `GET /random-photo` and `GET /camera/<entity>` endpoints — random
Immich photo fetch and HA camera snapshots, re-encoded via Pillow to
JPEGDEC-safe SOF0 baseline JPEGs — are now served by **panel-hub**
(`panel-hub/`, port `:8768`). See the panel-hub section for details.

### Jellyfin proxy (`jellyfin-proxy/`)

A small Python service (default port **8767**) that talks to Jellyfin on
your LAN and exposes panel-friendly endpoints:

- `GET /health` — bridge health check used on Wi-Fi connect and page load
- `GET /movies?start=0&limit=8` — paginated movie list (JSON)
- `GET /poster/<item_id>` — poster re-encoded as SOF0 baseline JPEG
- `POST /play/<item_id>` — optional direct play on an active Jellyfin
  client (requires `JELLYFIN_PLAY_CLIENT` in `proxy.env`)

Config: copy `proxy.env.example` → `proxy.env` with `JELLYFIN_URL` and
`JELLYFIN_API_KEY` (Dashboard → Advanced → API Keys).

**Playback note:** The ESP32-P4 panel has no H.264/HEVC video decoder.
The Jellyfin page browses your library and shows posters; tapping **Play**
calls `script.jellyfin_play_on_panel` in Home Assistant so you can route
playback to a TV, Shield, Chromecast, or the panel speaker (audio-only).
Edit the script in `home_assistant/packages/esp32p4_panel.yaml`.

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
│   ├── theme_palettes.yaml             ← Five palettes, apply_panel_theme, voice UI colours
│   ├── audio_voice_board.yaml          ← I2S codec, microWakeWord, voice assistant
│   ├── tuya_local.yaml                 ← Tuya LAN page + HTTP scripts
│   ├── jellyfin_local.yaml             ← Jellyfin movie browse page
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

- **Don't run parallel Tuya HTTP on the Tuya page.** Firing six poll
  scripts at once (~10 concurrent GETs) and calling the 40-step
  `tuya_apply_ui` after every response used to freeze the panel and
  trigger watchdog crashes. Polls are now sequential with a single UI
  apply at the end; `g_tuya_busy` blocks taps during in-flight requests.

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
