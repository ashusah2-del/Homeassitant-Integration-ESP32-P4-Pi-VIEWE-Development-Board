# Changelog

Panel firmware version (`firmware_version` substitution in `mangalam-panel.yaml`
and `mangalam-panel-2.yaml`) is shown on the Settings page ("Firmware: vX.Y.Z")
and the slideshow's "Layout vX.Y.Z" pill. Bump it whenever a change is
user-visible enough to matter for support/debugging — not required for every
commit.

## [0.7.2] - 2026-07-23

Fixed: tapping dashboard room tiles, tado thermostat cards, or dragging
the brightness slider's release did nothing on panel 2 — confirmed
touch input was accurate, but the LVGL `on_click`/`on_release` event
itself never fired. Root cause: LVGL 8.4.0's flex `ROW_WRAP` layout
doesn't reliably hit-test percentage-sized (`32%`/`48%`) children for
click purposes. Fixed by switching `room1-6_card`, `tado1-6_card`
(`display_touch_board.yaml`) and `ih_sw1_card`/`ih_sw2_card`
(`ihost_local.yaml`) from percentage sizing to fixed pixel sizing per
panel (new `dash_card_w`/`dash_card_h`/`ih_sw_card_w`/`ih_sw_card_h`
substitutions), while keeping the flex layout itself. Verified live via
a temporary diagnostic log that confirmed the click now fires reliably.

## [0.7.0] - 2026-07-22

Second physical panel (`mangalam-panel-2.yaml`) — a 10.1" Guition JC8012P4A1
(800x1280, rotated to 1280x800 landscape), distinct from the original 7"
panel (`mangalam-panel.yaml`, 1024x600). Both share the same ESPHome
packages; per-panel differences are substitution-driven.

- Display/touch driver parameterized (`lcd_model`, `lcd_reset_pin`,
  `touch_platform`, touch transform) so `packages/display_touch_board.yaml`
  serves both panels without forking.
- New panel's touch controller is a GSL3680 (not GT911) — vendored a local
  `esphome/my_components/gsl3680` component (from jtenniswood/espcontrol,
  originally kvj/esphome) with a bounds-check bug fix. Final working
  transform (`swap_xy:true`, no mirroring) found empirically and confirmed
  behaviorally, since the vendored driver reads I2C registers with axes
  transposed relative to the manufacturer's own reference driver.
- Slideshow photo endpoint (`panel-hub`'s `/random-photo`) gained optional
  `?w=&h=&fill=` query params so each panel requests a canvas matching its
  own slideshow area, with cover-crop instead of letterbox. Panel 1 passes
  its prior defaults explicitly — no behavior change there.
- Settings and Jellyfin pages resolution-adapted for the second panel's
  bigger screen (two-column layouts, footer control rows).
- Fixed: `slideshow_page` was scrollable by default outside of layout-edit
  mode, so swiping the photo dragged the calendar/weather/clock/wake pills
  along with it instead of leaving them at their configured positions.

## [0.7.1] - 2026-07-22

Resolution-fill follow-up for the second panel, plus a proxy bug fix
affecting both panels.

- `diagnostics_page`, `layout_settings_page`, `ihost_page`, and `tuya_page`
  converted from hand-placed 1024×600-sized absolute pixel layouts to
  flex/percentage containers (matching the pattern already proven on
  `dashboard_page`/`settings_page`), so they properly fill the screen on
  the 10.1" panel instead of leaving blank margins. `tuya_page`'s two
  5-socket button rows also converted to flex rows instead of per-button
  fixed x-offsets.
- Fixed: `panel-hub`'s `encode_sof0()` (and the deprecated `immich-proxy`'s
  equivalent) opened source JPEGs without applying `ImageOps.exif_transpose`,
  so photos carrying an EXIF `Orientation` tag (common on phone photos)
  were re-encoded with their raw sensor pixel data, appearing rotated
  90°/270° in the slideshow regardless of which panel displayed them.

## [0.6.0] - 2026-07-10

Design-token system, flex layouts, top-layer toast, central error handling
(commit `c4d6e29`). See `projects/home-assistant-p4-c6-panel/esphome/mangalam-panel.yaml`
substitutions for the full token set (`c_*` colours, `header_h`/`footer_h`/
`content_h`/`card_radius`/`nav_btn_w` metrics, etc).
