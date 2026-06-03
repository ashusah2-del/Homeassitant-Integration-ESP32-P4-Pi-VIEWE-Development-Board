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
- `secrets.yaml.example` template for Wi-Fi and encrypted API/OTA values

## How to use

1. Copy these files into your ESPHome config directory, or use this directory directly as your project root.
2. Copy `secrets.yaml.example` to `secrets.yaml` and fill all values.
3. Keep board settings in `guition_p4_7inch_compat.yaml` unless your hardware revision differs.
4. Review and adjust board mappings if your PCB revision differs.
5. Install from ESPHome and adopt the node in Home Assistant.

## Secret generation

- API key: `openssl rand -base64 32`
- OTA password: `openssl rand -base64 24`

## Important

- ESP32-P4 support in ESPHome may vary by ESPHome version.
- If your ESPHome build does not support this board target yet, keep using the ESP-IDF path for production and use this profile as a migration scaffold.
