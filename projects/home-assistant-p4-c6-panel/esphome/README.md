# ESPHome Compatibility Profile (Guition P4 7.0 Style)

This folder provides a compatibility profile so you can test an ESPHome-first path in parallel with the ESP-IDF firmware under `../firmware`.

## Purpose

- Keep your hardware project in one repository
- Try an ESPHome/Home Assistant native integration flow quickly
- Reuse a modular package structure similar to community P4 device layouts

## Files

- `guition_p4_7inch_compat.yaml` main ESPHome node file
- `packages/core_ha.yaml` HA API, OTA, logger, and baseline entities
- `packages/display_touch_board.yaml` board-mapped MIPI DSI display + GT911 touch config
- `packages/audio_voice_board.yaml` board-mapped I2S/audio codec + wake-word/voice config

## How to use

1. Copy these files into your ESPHome config directory, or use this directory directly as your project root.
2. Edit Wi-Fi, API key, OTA password, and board value in `guition_p4_7inch_compat.yaml`.
3. Review and adjust board mappings if your PCB revision differs.
4. Install from ESPHome and adopt the node in Home Assistant.

## Important

- ESP32-P4 support in ESPHome may vary by ESPHome version.
- If your ESPHome build does not support this board target yet, keep using the ESP-IDF path for production and use this profile as a migration scaffold.
