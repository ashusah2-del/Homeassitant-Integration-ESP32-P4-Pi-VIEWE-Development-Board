# ESP32-P4/C6 7-inch Home Assistant Panel

This project is a starter for the 7.0-inch 1024x600 IPS capacitive touch ESP32-P4 + ESP32-C6 development board with camera support and Home Assistant integration.

## What is included

- ESP-IDF firmware starter with:
  - Wi-Fi station mode
  - MQTT client
  - Home Assistant MQTT discovery messages
  - Device heartbeat and camera status topics
- Home Assistant package template
- OpenSCAD housing starter model with display and camera cutout
- Git helper scripts for frequent commit/push workflows

## Layout

- `firmware/` ESP-IDF application
- `home_assistant/` Home Assistant YAML templates
- `mechanical/housing/` OpenSCAD housing model
- `scripts/` helper scripts

## 1) Build firmware

Prerequisites:

- ESP-IDF v5.3+
- USB serial access to the board

Commands:

```bash
cd projects/home-assistant-p4-c6-panel/firmware
idf.py set-target esp32p4
idf.py menuconfig
idf.py -p /dev/ttyACM0 flash monitor
```

In `menuconfig` set:

- `Home Assistant Panel Settings -> WiFi SSID`
- `Home Assistant Panel Settings -> WiFi Password`
- `Home Assistant Panel Settings -> MQTT Broker URI`
- `Home Assistant Panel Settings -> MQTT Device Name`

## 2) Add Home Assistant package

Copy `home_assistant/packages/esp32p4_panel.yaml` into your HA `packages` folder and adjust the topic prefix if needed.

## 3) Housing

Open `mechanical/housing/esp32p4_7inch_case.scad` in OpenSCAD, adjust dimensions if your panel variant differs, then export STL.

## Notes

- Camera support is represented as MQTT-discovered camera status and stream URL text sensor in this starter. For full MJPEG/RTSP streaming, add your preferred camera streaming component in firmware and point Home Assistant to that stream.
- Touch and LVGL UI hooks are left in the firmware as integration points for your final UI logic.