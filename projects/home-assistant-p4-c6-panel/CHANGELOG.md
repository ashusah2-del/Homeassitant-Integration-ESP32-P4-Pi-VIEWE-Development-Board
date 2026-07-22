# Changelog

Panel firmware version (`firmware_version` substitution in `mangalam-panel.yaml`
and `mangalam-panel-2.yaml`) is shown on the Settings page ("Firmware: vX.Y.Z")
and the slideshow's "Layout vX.Y.Z" pill. Bump it whenever a change is
user-visible enough to matter for support/debugging — not required for every
commit.

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

## [0.6.0] - 2026-07-10

Design-token system, flex layouts, top-layer toast, central error handling
(commit `c4d6e29`). See `projects/home-assistant-p4-c6-panel/esphome/mangalam-panel.yaml`
substitutions for the full token set (`c_*` colours, `header_h`/`footer_h`/
`content_h`/`card_radius`/`nav_btn_w` metrics, etc).
