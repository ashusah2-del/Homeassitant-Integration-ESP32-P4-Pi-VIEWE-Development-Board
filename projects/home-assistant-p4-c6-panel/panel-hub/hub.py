#!/usr/bin/env python3
"""
panel-hub — unified proxy + AI hub for the Mangalam ESP32-P4 panel.

Replaces three separate services on one port (default 8768):
  immich-proxy  (was :8765)  GET /random-photo, GET /camera/<entity>
  jellyfin-proxy(was :8767)  GET /movies, GET /poster/<id>, POST /play/<id>
  tuya-bridge   (was :8766)  GET /devices, /simple/*, /strip/*, /lock/*, …

Adds:
  POST /ai/ask              — Ollama LLM, no cloud dependency
  POST /automation/calendar-refresh  — manual trigger
  Background task: calendar events pushed to HA every CALENDAR_REFRESH_HRS

AI / voice: Ollama only. No Claude, no OpenAI, no external API calls.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import threading
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image

try:
    import tinytuya
    _TUYA_OK = True
except ImportError:
    _TUYA_OK = False

load_dotenv("hub.env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("panel-hub")

# ── Config ────────────────────────────────────────────────────────────────────
IMMICH_URL      = os.getenv("IMMICH_URL", "").rstrip("/")
IMMICH_KEY      = os.getenv("IMMICH_API_KEY", "")
FAVORITES_ONLY  = os.getenv("FAVORITES_ONLY", "false").lower() in ("1", "true", "yes", "on")
PERSON_NAMES    = [p.strip() for p in os.getenv("IMMICH_PERSON_NAMES", "").split(",") if p.strip()]
LANDSCAPE_PREFER = os.getenv("LANDSCAPE_PREFER", "true").lower() in ("1", "true", "yes", "on")
PORTRAIT_BATCH   = int(os.getenv("PORTRAIT_BATCH", "4"))
PHOTO_MAX_W     = int(os.getenv("MAX_W", "800"))
PHOTO_MAX_H     = int(os.getenv("MAX_H", "480"))
JPEG_QUALITY    = int(os.getenv("JPEG_QUALITY", "78"))
RETRIES         = int(os.getenv("RETRIES", "8"))

HA_URL          = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN        = os.getenv("HA_TOKEN", "")
HA_HEADERS      = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

JELLYFIN_URL         = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_KEY         = os.getenv("JELLYFIN_API_KEY", "")
JELLYFIN_USER_ID     = os.getenv("JELLYFIN_USER_ID", "").strip()
JELLYFIN_PLAY_CLIENT = os.getenv("JELLYFIN_PLAY_CLIENT", "").strip()
FIRETV_ADB_CONTAINER = os.getenv("FIRETV_ADB_CONTAINER", "adb-server")
FIRETV_ADB_DEVICE    = os.getenv("FIRETV_ADB_DEVICE", "192.168.55.77:5555")
POSTER_MAX_W    = int(os.getenv("POSTER_MAX_W", "280"))
POSTER_MAX_H    = int(os.getenv("POSTER_MAX_H", "400"))
POSTER_QUALITY  = int(os.getenv("POSTER_QUALITY", "85"))

OLLAMA_URL      = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")

IHOST_URL   = os.getenv("IHOST_URL", "http://ihost.local").rstrip("/")
IHOST_TOKEN = os.getenv("IHOST_TOKEN", "")

DEVICES_FILE       = os.getenv("DEVICES_FILE", "devices.json")
PANEL_CONFIG_FILE  = os.getenv("PANEL_CONFIG_FILE", "panel_config.json")
HUB_PORT           = int(os.getenv("HUB_PORT", "8768"))
CAL_REFRESH_HRS    = int(os.getenv("CALENDAR_REFRESH_HRS", "1"))

# ── Image helpers ─────────────────────────────────────────────────────────────

def _sof_type(data: bytes) -> int | None:
    for i in range(min(len(data) - 1, 4095)):
        if data[i] == 0xFF and 0xC0 <= data[i + 1] <= 0xCF and data[i + 1] not in (0xC4, 0xC8, 0xCC):
            return data[i + 1]
    return None


def encode_sof0(raw: bytes, max_w: int, max_h: int,
                quality: int = JPEG_QUALITY, subsampling: int = 2) -> bytes:
    """Re-encode to SOF0 baseline JPEG on a 16-pixel-aligned canvas."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    cw = max(16, (max_w // 16) * 16)
    ch = max(16, (max_h // 16) * 16)
    img.thumbnail((cw, ch), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (cw, ch), (0, 0, 0))
    canvas.paste(img, ((cw - img.width) // 2, (ch - img.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=quality,
                optimize=False, progressive=False, subsampling=subsampling)
    return buf.getvalue()


# ── Immich ────────────────────────────────────────────────────────────────────

_person_ids: list[tuple[str, str]] | None = None  # (name, id) pairs


def _resolve_person_ids() -> list[tuple[str, str]]:
    global _person_ids
    if _person_ids is not None:
        return _person_ids
    if not PERSON_NAMES:
        _person_ids = []
        return _person_ids
    try:
        req = urllib.request.Request(
            f"{IMMICH_URL}/api/people",
            headers={"x-api-key": IMMICH_KEY, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        people = data.get("people", data) if isinstance(data, dict) else data
        resolved: list[tuple[str, str]] = []
        for wanted in PERSON_NAMES:
            wl = wanted.lower()
            match = next((p for p in people if wl == p.get("name", "").strip().lower()), None)
            match = match or next((p for p in people if wl in p.get("name", "").strip().lower()), None)
            if match:
                resolved.append((match.get("name") or wanted, match["id"]))
            else:
                log.warning("Immich person %r not found", wanted)
        _person_ids = resolved
        log.info("Immich people filter: %s", ", ".join(f"{n}={i}" for n, i in _person_ids))
        return _person_ids
    except Exception as e:
        log.warning("Person ID resolve failed (will retry): %s", e)
        return []


async def fetch_random_photo() -> bytes | None:
    loop = asyncio.get_event_loop()
    person_filter = await loop.run_in_executor(None, _resolve_person_ids)

    for attempt in range(RETRIES):
        try:
            search: dict[str, Any] = {
                "size": PORTRAIT_BATCH if LANDSCAPE_PREFER else 1,
                "type": "IMAGE",
            }
            person_name = None
            if PERSON_NAMES:
                if not person_filter:
                    log.error("people filter set but no IDs resolved")
                    return None
                person_name, person_id = random.choice(person_filter)
                search["personIds"] = [person_id]
            if FAVORITES_ONLY:
                search["isFavorite"] = True

            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{IMMICH_URL}/api/search/random",
                    headers={"x-api-key": IMMICH_KEY, "Accept": "application/json"},
                    json=search)
                r.raise_for_status()
                assets = r.json()

            if not isinstance(assets, list) or not assets:
                log.warning("attempt %d: empty random result", attempt)
                continue

            images = [a for a in assets if a.get("type", "IMAGE") == "IMAGE" and a.get("id")]
            if not images:
                continue

            if LANDSCAPE_PREFER:
                landscape = [a for a in images if (a.get("width") or 0) >= (a.get("height") or 0)]
                item = (landscape or images)[0]
            else:
                item = images[0]

            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{IMMICH_URL}/api/assets/{item['id']}/thumbnail",
                    headers={"x-api-key": IMMICH_KEY},
                    params={"size": "preview"})
                r.raise_for_status()
                raw = r.content

            sof = _sof_type(raw)
            if sof is None:
                log.warning("attempt %d: no SOF marker", attempt)
                continue

            jpeg = await loop.run_in_executor(
                None, encode_sof0, raw, PHOTO_MAX_W, PHOTO_MAX_H, JPEG_QUALITY, 2)

            orient = "landscape" if (item.get("width", 0) >= item.get("height", 0)) else "portrait-fallback"
            scope = f"person:{person_name}" if person_name else ("favorites" if FAVORITES_ONLY else "all")
            log.info("photo %s (%s, %s) SOF 0xFF%02X: %d→%d bytes",
                     item["id"], scope, orient, sof, len(raw), len(jpeg))
            return jpeg

        except Exception as e:
            log.warning("Immich attempt %d: %s", attempt, e)
            await asyncio.sleep(1)

    log.error("Immich: all %d attempts failed", RETRIES)
    return None


async def fetch_camera_snapshot(entity: str, max_w: int, max_h: int) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{HA_URL}/api/camera_proxy/camera.{entity}",
                headers=HA_HEADERS)
            r.raise_for_status()
        loop = asyncio.get_event_loop()
        # 4:4:4 subsampling (subsampling=0) is safer for JPEGDEC on camera images
        jpeg = await loop.run_in_executor(
            None, encode_sof0, r.content, max_w, max_h, JPEG_QUALITY, 0)
        log.info("camera %s: %d→%d bytes", entity, len(r.content), len(jpeg))
        return jpeg
    except Exception as e:
        log.error("camera %s: %s", entity, e)
        return None


# ── Jellyfin ──────────────────────────────────────────────────────────────────

_jf_user_id: str = ""


async def _get_jf_user_id() -> str:
    global _jf_user_id
    if JELLYFIN_USER_ID:
        return JELLYFIN_USER_ID
    if _jf_user_id:
        return _jf_user_id
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{JELLYFIN_URL}/Users",
                              headers={"X-Emby-Token": JELLYFIN_KEY})
        r.raise_for_status()
        users = r.json()
    if not users:
        raise RuntimeError("No Jellyfin users found")
    _jf_user_id = users[0]["Id"]
    log.info("Jellyfin user: %s (%s)", users[0].get("Name"), _jf_user_id)
    return _jf_user_id


def _jf_display_name(item: dict) -> str:
    return (item.get("OriginalTitle") or item.get("Name") or "Unknown").strip()


async def fetch_movies(start: int, limit: int) -> dict:
    uid = await _get_jf_user_id()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{JELLYFIN_URL}/Users/{uid}/Items",
            headers={"X-Emby-Token": JELLYFIN_KEY},
            params={
                "Recursive": "true", "IncludeItemTypes": "Movie",
                "SortBy": "PremiereDate", "SortOrder": "Descending",
                "Fields": "ProductionYear,OriginalTitle,PremiereDate",
                "StartIndex": max(0, start), "Limit": max(1, min(limit, 20)),
            })
        r.raise_for_status()
        data = r.json()
    return {
        "total": int(data.get("TotalRecordCount", 0)),
        "start": max(0, start),
        "movies": [{"id": i["Id"], "name": _jf_display_name(i),
                    "year": i.get("ProductionYear") or 0}
                   for i in data.get("Items", [])],
    }


async def fetch_poster(item_id: str) -> bytes | None:
    loop = asyncio.get_event_loop()
    async with httpx.AsyncClient(timeout=15) as client:
        for img_type in ("Primary", "Poster"):
            try:
                r = await client.get(
                    f"{JELLYFIN_URL}/Items/{urllib.parse.quote(item_id)}/Images/{img_type}",
                    headers={"X-Emby-Token": JELLYFIN_KEY, "Accept": "image/jpeg"},
                    params={"maxHeight": POSTER_MAX_H, "maxWidth": POSTER_MAX_W,
                            "quality": POSTER_QUALITY, "format": "jpg"})
                r.raise_for_status()
                if r.content:
                    return await loop.run_in_executor(
                        None, encode_sof0, r.content, POSTER_MAX_W, POSTER_MAX_H,
                        POSTER_QUALITY, 2)
            except Exception:
                continue
    log.warning("poster not found for %s", item_id)
    return None


# ── Tuya ──────────────────────────────────────────────────────────────────────

_tuya_config: dict = {"wifi_devices": [], "gateways": []}
_tuya_lock = threading.Lock()


def _load_tuya_devices() -> None:
    global _tuya_config
    if not os.path.exists(DEVICES_FILE):
        log.warning("devices.json not found — Tuya endpoints will return empty")
        return
    with open(DEVICES_FILE) as f:
        _tuya_config = json.load(f)
    n_wifi = len(_tuya_config.get("wifi_devices", []))
    n_gw   = len(_tuya_config.get("gateways", []))
    n_sub  = sum(len(g.get("sub_devices", [])) for g in _tuya_config.get("gateways", []))
    log.info("Tuya: %d wifi, %d gateways, %d sub-devices", n_wifi, n_gw, n_sub)


def _find_device(device_id: str):
    with _tuya_lock:
        cfg = _tuya_config
    for d in cfg.get("wifi_devices", []):
        if d["id"] == device_id:
            return "wifi", d, None
    for gw in cfg.get("gateways", []):
        if gw["id"] == device_id:
            return "gateway", gw, None
        for sub in gw.get("sub_devices", []):
            if sub["id"] == device_id:
                return "sub", sub, gw
    return None, None, None


def _needs_key(cfg: dict) -> bool:
    return cfg.get("local_key", "REPLACE_ME") == "REPLACE_ME"


def _make_device(kind: str, dev_cfg: dict, gw_cfg: dict | None):
    if not _TUYA_OK:
        raise RuntimeError("tinytuya not installed")
    if kind in ("wifi", "gateway"):
        d = tinytuya.OutletDevice(
            dev_id=dev_cfg["id"], address=dev_cfg["ip"],
            local_key=dev_cfg["local_key"],
            version=float(dev_cfg.get("version", "3.3")))
    else:
        gw = tinytuya.Device(
            dev_id=gw_cfg["id"], address=gw_cfg["ip"],
            local_key=gw_cfg["local_key"],
            version=float(gw_cfg.get("version", "3.4")))
        gw.set_socketTimeout(3)
        d = tinytuya.Device(
            dev_id=dev_cfg["id"], address=gw_cfg["ip"],
            local_key=gw_cfg["local_key"],
            version=float(gw_cfg.get("version", "3.4")),
            parent=gw)
    d.set_socketTimeout(3)
    d.set_sendWait(0.5)
    return d


def _tuya_get_dps(device_id: str) -> dict:
    kind, dev_cfg, gw_cfg = _find_device(device_id)
    if dev_cfg is None:
        raise ValueError(f"Unknown device: {device_id}")
    key_cfg = gw_cfg if kind == "sub" else dev_cfg
    if _needs_key(key_cfg):
        raise RuntimeError("Local key not configured")
    d = _make_device(kind, dev_cfg, gw_cfg)
    result = d.status()
    if result is None or "Error" in result:
        raise RuntimeError(result.get("Error", "No response") if result else "No response")
    return result.get("dps", {})


def _tuya_set_dps(device_id: str, dps_num: int, value) -> tuple[bool, str]:
    kind, dev_cfg, gw_cfg = _find_device(device_id)
    if dev_cfg is None:
        return False, f"Unknown device: {device_id}"
    key_cfg = gw_cfg if kind == "sub" else dev_cfg
    if _needs_key(key_cfg):
        return False, "Local key not configured"
    try:
        d = _make_device(kind, dev_cfg, gw_cfg)
        result = d.set_status(value, switch=dps_num)
        if isinstance(result, dict) and "Error" in result:
            return False, result["Error"]
        return True, "ok"
    except Exception as e:
        return False, str(e)


def _tuya_list_devices() -> list:
    with _tuya_lock:
        cfg = _tuya_config
    out = []
    for d in cfg.get("wifi_devices", []):
        out.append({"id": d["id"], "name": d["name"], "type": d.get("type", "simple"),
                    "ip": d["ip"], "configured": not _needs_key(d)})
    for gw in cfg.get("gateways", []):
        out.append({"id": gw["id"], "name": gw["name"], "type": "gateway",
                    "ip": gw["ip"], "configured": not _needs_key(gw)})
        for sub in gw.get("sub_devices", []):
            out.append({"id": sub["id"], "name": sub["name"],
                        "type": sub.get("type", "simple"),
                        "gateway": gw["id"], "configured": not _needs_key(gw)})
    return out


def _tuya_update_keys(updates: list) -> list:
    with _tuya_lock:
        updated = []
        for upd in updates:
            did, key = upd.get("id"), upd.get("key")
            if not did or not key:
                continue
            for d in _tuya_config.get("wifi_devices", []):
                if d["id"] == did:
                    d["local_key"] = key
                    updated.append(did)
                    break
            for gw in _tuya_config.get("gateways", []):
                if gw["id"] == did:
                    gw["local_key"] = key
                    updated.append(did)
                    break
        with open(DEVICES_FILE, "w") as f:
            json.dump(_tuya_config, f, indent=2)
    return updated


# ── Panel config ──────────────────────────────────────────────────────────────

_DEFAULT_CAMERAS = [
    {"slot": 0, "label": "Front Door", "entity": "front_door"},
    {"slot": 1, "label": "Front Bell", "entity": "front_door_bell"},
    {"slot": 2, "label": "Side Door",  "entity": "side_door"},
    {"slot": 3, "label": "Garden",     "entity": "garden"},
]


def _load_panel_config() -> dict:
    if os.path.exists(PANEL_CONFIG_FILE):
        try:
            with open(PANEL_CONFIG_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.error("panel_config load failed: %s", e)
    return {"cameras": _DEFAULT_CAMERAS}


# ── Calendar refresh ──────────────────────────────────────────────────────────

async def refresh_calendar() -> None:
    if not HA_URL or not HA_TOKEN:
        log.warning("Calendar refresh skipped: HA_URL/HA_TOKEN not set")
        return
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = today + timedelta(days=7, hours=23, minutes=59, seconds=59)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{HA_URL}/api/states", headers=HA_HEADERS)
            r.raise_for_status()
            cal_ids = [s["entity_id"] for s in r.json()
                       if s["entity_id"].startswith("calendar.")]

            lines: list[str] = []
            for cal_id in cal_ids:
                r2 = await client.get(
                    f"{HA_URL}/api/calendars/{cal_id}",
                    headers=HA_HEADERS,
                    params={"start": today.isoformat(),
                            "end": week_end.isoformat()})
                if r2.status_code != 200:
                    continue
                for evt in r2.json():
                    start = evt.get("start", {})
                    dt_str = start.get("dateTime") or start.get("date", "")
                    summary = evt.get("summary", "")
                    if "T" in dt_str:
                        dt = datetime.fromisoformat(dt_str)
                        line = f"{dt.strftime('%a %H:%M')} {summary}"
                    else:
                        dt = datetime.fromisoformat(dt_str)
                        line = f"{dt.strftime('%a')} All Day: {summary}"
                    lines.append(line[:40])

            lines.sort()
            value = "\n".join(lines)[:254] if lines else "No upcoming events"
            await client.post(
                f"{HA_URL}/api/services/input_text/set_value",
                headers=HA_HEADERS,
                json={"entity_id": "input_text.panel_calendar_events", "value": value})
        log.info("Calendar refreshed: %d events", len(lines))
    except Exception as e:
        log.error("Calendar refresh failed: %s", e)


async def _calendar_loop() -> None:
    while True:
        await refresh_calendar()
        await asyncio.sleep(CAL_REFRESH_HRS * 3600)


# ── HA helper ─────────────────────────────────────────────────────────────────

async def ha_call_service(service: str, data: dict) -> bool:
    domain, svc = service.split(".", 1)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{HA_URL}/api/services/{domain}/{svc}",
                headers=HA_HEADERS, json=data)
            return r.status_code < 300
    except Exception as e:
        log.error("HA %s: %s", service, e)
        return False


# ── Ollama AI ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a smart home assistant for a house in the UK called Mangalam.
You control Home Assistant. Reply ONLY with valid JSON — no prose outside the JSON object.

Response format:
{
  "response": "Brief reply to speak back to the user (1-2 sentences max)",
  "action": {
    "service": "light.turn_on",
    "entity_id": "light.bedroom_lights",
    "data": {}
  }
}
Set "action" to null when no HA action is needed.

Known entities (use these exact IDs):
Lights: light.drawing_room_light, light.office_lights, light.hallway, light.stairs_light, light.bedroom_lights, switch.conservatory_switch, switch.drawinglights
Climate (Tado): climate.drawing_room, climate.office, climate.main_bedroom, climate.mukta, climate.advik, climate.heating
Media: media_player.lg_webos_tv_58b2 (Drawing Room TV), media_player.ashok_s_fire_tv_cube (Fire TV)
Scenes: scene.evening, scene.morning

Use the "context" field (current panel page, active room) to resolve ambiguous commands.
"""


async def ask_ollama(text: str, context: dict) -> dict:
    prompt = f"Context: {json.dumps(context)}\nCommand: {text}"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    "format": "json",
                    "stream": False,
                })
            r.raise_for_status()
    except Exception as e:
        raise HTTPException(502, f"Ollama unreachable: {e}")

    content = r.json().get("message", {}).get("content", "{}")
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        result = {"response": content, "action": None}

    action = result.get("action")
    action_taken = False
    if isinstance(action, dict) and action.get("service"):
        svc_data = dict(action.get("data") or {})
        if action.get("entity_id"):
            svc_data["entity_id"] = action["entity_id"]
        action_taken = await ha_call_service(action["service"], svc_data)
        log.info("AI action: %s %s → %s",
                 action["service"], svc_data.get("entity_id", ""), action_taken)

    return {"response": result.get("response", "Done."), "action_taken": action_taken}


# ── Tuya web dashboard ────────────────────────────────────────────────────────

_TUYA_DASHBOARD = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tuya Local Bridge — panel-hub</title>
  <style>
    :root{color-scheme:dark;--bg:#071008;--card:#102016;--line:#27543a;--ok:#69f0ae;--warn:#ffb74d;--bad:#ff5252;--muted:#9fb0a6;--text:#f4fff8}
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(circle at top,#12301f,var(--bg));color:var(--text)}
    header{position:sticky;top:0;z-index:2;background:rgba(7,16,8,.92);border-bottom:1px solid var(--line);padding:16px 20px;backdrop-filter:blur(8px)}
    h1{margin:0;font-size:clamp(24px,4vw,36px);color:var(--ok)}
    header p{margin:6px 0 0;color:var(--muted)}
    main{max-width:1180px;margin:0 auto;padding:18px}
    .toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:16px}
    button{border:0;border-radius:10px;padding:10px 14px;background:#1b5e20;color:#fff;font-weight:700;cursor:pointer}
    button.secondary{background:#1e3a5f}
    button.danger{background:#6a1b1a}
    button:disabled{cursor:not-allowed;opacity:.45}
    .pill{display:inline-flex;align-items:center;gap:8px;padding:8px 11px;border-radius:999px;border:1px solid var(--line);color:var(--muted);background:rgba(16,32,22,.75)}
    .dot{width:10px;height:10px;border-radius:50%;background:var(--warn)}
    .dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px}
    .card{background:rgba(16,32,22,.93);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:0 8px 22px rgba(0,0,0,.22)}
    .top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
    h2{margin:0;font-size:20px}
    .meta{margin-top:4px;color:var(--muted);font-size:13px;overflow-wrap:anywhere}
    .status{margin:14px 0;min-height:24px;font-size:16px}
    .status.ok{color:var(--ok)}.status.warn{color:var(--warn)}.status.bad{color:var(--bad)}
    .controls{display:flex;gap:8px;flex-wrap:wrap}
    .strip-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
    .strip-row button{padding:10px 4px}
  </style>
</head>
<body>
<header>
  <h1>Tuya Local Bridge</h1>
  <p>panel-hub · Local LAN Tuya control · no cloud calls</p>
</header>
<main>
  <div class="toolbar">
    <button onclick="loadDevices()">Refresh devices</button>
    <button class="secondary" onclick="refreshAll()">Refresh all status</button>
    <span class="pill"><span id="hdot" class="dot"></span><span id="htext">Checking…</span></span>
  </div>
  <section id="devices" class="grid"></section>
</main>
<script>
const state={devices:[]};
function el(tag,attrs={},children=[]){const n=document.createElement(tag);for(const[k,v]of Object.entries(attrs)){if(k==="class")n.className=v;else if(k==="text")n.textContent=v;else if(k.startsWith("on"))n.addEventListener(k.slice(2),v);else n.setAttribute(k,v);}for(const c of children)n.append(c);return n;}
async function api(path,opts={},ms=6500){const ac=new AbortController();const t=setTimeout(()=>ac.abort(),ms);try{const r=await fetch(path,{...opts,signal:ac.signal});const d=await r.json().catch(()=>({}));return{ok:r.ok,status:r.status,data:d};}finally{clearTimeout(t);}}
function setHealth(ok,text){document.getElementById("hdot").className=`dot ${ok?"ok":"bad"}`;document.getElementById("htext").textContent=text;}
async function checkHealth(){try{const r=await api("/health",{},2500);setHealth(r.ok&&r.data.ok,"Hub online");}catch{setHealth(false,"Hub offline");}}
async function loadDevices(){await checkHealth();const root=document.getElementById("devices");root.textContent="Loading…";try{const r=await api("/devices",{},4000);if(!r.ok)throw new Error(r.data.error||`HTTP ${r.status}`);state.devices=r.data;root.textContent="";for(const d of state.devices)root.append(renderCard(d));refreshAll();}catch(e){root.textContent=`Error: ${e.message}`;}}
function renderCard(device){const card=el("article",{class:"card",id:`dev-${device.id}`});const top=el("div",{class:"top"},[el("div",{},[el("h2",{text:device.name||device.id}),el("div",{class:"meta",text:`${device.type||"device"} | ${device.ip?`IP ${device.ip}`:`gw ${device.gateway||"-"}`} | ${device.id}`})]),el("span",{class:"pill",text:device.configured?"configured":"missing key"})]);const status=el("div",{class:"status warn",id:`status-${device.id}`,text:"Not polled yet"});const controls=el("div",{class:device.type==="strip"?"strip-row":"controls",id:`controls-${device.id}`});fillControls(device,controls);card.append(top,status,controls);return card;}
function fillControls(device,controls){controls.textContent="";if(!device.configured){controls.append(el("span",{class:"status bad",text:"Local key not configured"}));return;}if(device.type==="gateway"){controls.append(el("button",{class:"secondary",text:"Refresh",onclick:()=>refreshDevice(device)}));}else if(device.type==="strip"){for(const k of["s1","s2","s3","s4","usb"])controls.append(el("button",{text:k.toUpperCase(),onclick:()=>toggleStrip(device,k)}));}else if(device.type==="lock"){controls.append(el("button",{text:"Lock",onclick:()=>post(`/lock/${device.id}/lock`,device)}),el("button",{class:"danger",text:"Unlock",onclick:()=>post(`/lock/${device.id}/unlock`,device)}));}else{controls.append(el("button",{text:"On",onclick:()=>post(`/simple/${device.id}/on`,device)}),el("button",{class:"danger",text:"Off",onclick:()=>post(`/simple/${device.id}/off`,device)}));}}
function endpoint(d){const m={strip:`/strip/${d.id}`,lock:`/lock/${d.id}`,alarm:`/alarm/${d.id}`,sensor:`/sensor/${d.id}`,simple:`/simple/${d.id}`};return m[d.type]||null;}
function setStatus(device,cls,text){const n=document.getElementById(`status-${device.id}`);if(n){n.className=`status ${cls}`;n.textContent=text;}}
async function refreshDevice(device){const ep=endpoint(device);if(!ep){setStatus(device,"warn","Gateway — no direct state");return;}setStatus(device,"warn","Polling…");try{const r=await api(ep);const d=r.data||{};if(!r.ok||!d.ok){setStatus(device,"bad",d.error||`HTTP ${r.status}`);return;}if(device.type==="strip")setStatus(device,"ok",`S1 ${oo(d.s1)} S2 ${oo(d.s2)} S3 ${oo(d.s3)} S4 ${oo(d.s4)} USB ${oo(d.usb)}`);else if(device.type==="lock")setStatus(device,d.locked?"bad":"ok",d.locked?"Locked":"Unlocked");else if(device.type==="alarm")setStatus(device,d.alarm?"bad":"ok",d.alarm?"ALARM":"Clear");else if(device.type==="sensor")setStatus(device,d.state?"bad":"ok",d.state?"Active":"Clear");else setStatus(device,d.on?"ok":"warn",d.on?"ON":"OFF");}catch(e){setStatus(device,"bad",e.name==="AbortError"?"Timed out":e.message);}}
async function post(path,device){setStatus(device,"warn","Sending…");try{const r=await api(path,{method:"POST"});if(!r.ok||!r.data.ok){setStatus(device,"bad",r.data.message||`HTTP ${r.status}`);return;}await refreshDevice(device);}catch(e){setStatus(device,"bad",e.message);}}
async function toggleStrip(device,key){const st=document.getElementById(`status-${device.id}`)?.textContent||"";const on=new RegExp(`${key.toUpperCase()} ON`).test(st);await post(`/strip/${device.id}/${key}/${on?"off":"on"}`,device);}
async function refreshAll(){await checkHealth();for(const d of state.devices)refreshDevice(d);}
function oo(v){return v?"ON":"OFF";}
loadDevices();
</script>
</body>
</html>"""


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_tuya_devices()
    asyncio.create_task(_calendar_loop())
    yield


app = FastAPI(title="panel-hub", lifespan=lifespan)


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# ── Shared health (Jellyfin + Tuya both call /health) ────────────────────────

@app.get("/health")
async def health():
    return {"ok": True, "hub": "panel-hub"}


# ── Immich ────────────────────────────────────────────────────────────────────

@app.get("/random-photo")
async def random_photo():
    jpeg = await fetch_random_photo()
    if not jpeg:
        raise HTTPException(503, "Immich unavailable")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/camera/{entity:path}")
async def camera(entity: str, size: str = Query("thumb")):
    max_w, max_h = (800, 500) if size == "full" else (300, 200)
    jpeg = await fetch_camera_snapshot(entity, max_w, max_h)
    if not jpeg:
        raise HTTPException(503, "Camera unavailable")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ── Jellyfin ──────────────────────────────────────────────────────────────────

@app.get("/movies")
async def movies(start: int = Query(0), limit: int = Query(8)):
    try:
        return await fetch_movies(start, limit)
    except Exception as e:
        raise HTTPException(503, str(e))


@app.get("/poster/{item_id}")
async def poster(item_id: str):
    jpeg = await fetch_poster(item_id)
    if not jpeg:
        raise HTTPException(404, "Poster not found")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"})


@app.post("/play/{item_id}")
async def play(item_id: str):
    if not JELLYFIN_PLAY_CLIENT:
        return {"ok": False, "message": "JELLYFIN_PLAY_CLIENT not configured"}
    needle = JELLYFIN_PLAY_CLIENT.lower()
    # Retry for up to 15s — app may still be launching when HA calls us
    target = None
    for attempt in range(5):
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{JELLYFIN_URL}/Sessions",
                                  headers={"X-Emby-Token": JELLYFIN_KEY})
            r.raise_for_status()
            sessions = r.json()
        target = next((s for s in sessions if needle in
                       (s.get("DeviceName", "") + s.get("Client", "")).lower()), None)
        if target:
            break
        log.info("Jellyfin session '%s' not found yet (attempt %d/5), retrying…", JELLYFIN_PLAY_CLIENT, attempt + 1)
        await asyncio.sleep(3)
    if not target:
        raise HTTPException(404, f"No active Jellyfin session matching '{JELLYFIN_PLAY_CLIENT}' after retries")
    try:
        params: dict[str, Any] = {
            "playCommand": "PlayNow",
            "itemIds": item_id,
            "startPositionTicks": 0,
            "controllingUserId": JELLYFIN_USER_ID or target.get("UserId", ""),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{JELLYFIN_URL}/Sessions/{target['Id']}/Playing",
                headers={"X-Emby-Token": JELLYFIN_KEY},
                params=params)
            if not r.is_success:
                log.error("Jellyfin play %d: %s", r.status_code, r.text)
                r.raise_for_status()
        log.info("Jellyfin play %s on %s", item_id, target.get("DeviceName"))
        return {"ok": True, "session": target["Id"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/jellyfin/cast/{item_id}")
async def jellyfin_cast(item_id: str):
    """Launch Jellyfin on Fire TV via ADB, then push the item via Sessions API."""
    # 1. Wake Fire TV and open Jellyfin app via the adb-server Docker container
    def _adb_launch() -> str:
        try:
            import docker as docker_sdk
            client = docker_sdk.from_env()
            container = client.containers.get(FIRETV_ADB_CONTAINER)
            ec, out = container.exec_run(
                f"adb -s {FIRETV_ADB_DEVICE} shell am start -n org.jellyfin.androidtv/.ui.startup.StartupActivity",
                demux=False)
            return out.decode(errors="replace").strip() if out else f"exit {ec}"
        except Exception as ex:
            return f"error: {ex}"
    result_msg = await asyncio.get_event_loop().run_in_executor(None, _adb_launch)
    log.info("ADB launch Jellyfin: %s", result_msg)

    # 2. Poll Jellyfin Sessions until the Fire TV client appears (up to 20s)
    needle = (JELLYFIN_PLAY_CLIENT or "jellyfin android tv").lower()
    target = None
    for attempt in range(7):
        await asyncio.sleep(3)
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"{JELLYFIN_URL}/Sessions",
                                      headers={"X-Emby-Token": JELLYFIN_KEY})
                r.raise_for_status()
                sessions = r.json()
            target = next((s for s in sessions if needle in
                           (s.get("DeviceName", "") + s.get("Client", "")).lower()), None)
            if target:
                break
        except Exception:
            pass
        log.info("Waiting for Jellyfin Fire TV session (attempt %d/7)…", attempt + 1)

    if not target:
        raise HTTPException(404, "Jellyfin Fire TV session not found after 21s")

    # 3. Push the item to the active session
    # Jellyfin /Sessions/{id}/Playing takes query params, not a JSON body
    try:
        params: dict[str, Any] = {
            "playCommand": "PlayNow",
            "itemIds": item_id,
            "startPositionTicks": 0,
            "controllingUserId": JELLYFIN_USER_ID or target.get("UserId", ""),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{JELLYFIN_URL}/Sessions/{target['Id']}/Playing",
                headers={"X-Emby-Token": JELLYFIN_KEY},
                params=params)
            if not r.is_success:
                log.error("Jellyfin cast Sessions/Playing %d: %s", r.status_code, r.text)
                r.raise_for_status()
        log.info("Jellyfin cast %s → %s", item_id, target.get("DeviceName"))
        return {"ok": True, "session": target["Id"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


# ── Tuya dashboard ────────────────────────────────────────────────────────────

@app.get("/")
async def tuya_dashboard():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_TUYA_DASHBOARD)


# ── Tuya REST ─────────────────────────────────────────────────────────────────

@app.get("/devices")
async def tuya_devices():
    return _tuya_list_devices()


@app.get("/simple/{device_id}")
async def simple_status(device_id: str):
    _, dev_cfg, gw_cfg = _find_device(device_id)
    if dev_cfg is None:
        raise HTTPException(404, {"ok": False, "error": "Device not found"})
    loop = asyncio.get_event_loop()
    try:
        dps = await loop.run_in_executor(None, _tuya_get_dps, device_id)
        dps_num = dev_cfg.get("dps_on", 1)
        on = bool(dps.get(str(dps_num), dps.get(dps_num, False)))
        return {"ok": True, "on": on}
    except Exception as e:
        return JSONResponse({"ok": False, "on": False, "error": str(e)}, status_code=503)


@app.post("/simple/{device_id}/{action}")
async def simple_control(device_id: str, action: str):
    if action not in ("on", "off"):
        raise HTTPException(400, "action must be on or off")
    _, dev_cfg, _ = _find_device(device_id)
    if dev_cfg is None:
        raise HTTPException(404, {"ok": False, "error": "Device not found"})
    dps_num = dev_cfg.get("dps_on", 1)
    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(
        None, _tuya_set_dps, device_id, dps_num, action == "on")
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 503)


@app.get("/strip/{device_id}")
async def strip_status(device_id: str):
    _, dev_cfg, _ = _find_device(device_id)
    if dev_cfg is None:
        raise HTTPException(404, {"ok": False, "error": "Device not found"})
    loop = asyncio.get_event_loop()
    try:
        dps = await loop.run_in_executor(None, _tuya_get_dps, device_id)
        dmap = dev_cfg.get("dps_map", {"s1": 1, "s2": 2, "s3": 3, "s4": 4, "usb": 7})
        result: dict = {"ok": True}
        for k, v in dmap.items():
            result[k] = bool(dps.get(str(v), dps.get(v, False)))
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "s1": False, "s2": False, "s3": False,
                             "s4": False, "usb": False, "error": str(e)}, status_code=503)


@app.post("/strip/{device_id}/{socket_key}/{action}")
async def strip_control(device_id: str, socket_key: str, action: str):
    if action not in ("on", "off"):
        raise HTTPException(400, "action must be on or off")
    _, dev_cfg, _ = _find_device(device_id)
    if dev_cfg is None:
        raise HTTPException(404, {"ok": False, "error": "Device not found"})
    dmap = dev_cfg.get("dps_map", {"s1": 1, "s2": 2, "s3": 3, "s4": 4, "usb": 7})
    dps_num = dmap.get(socket_key)
    if dps_num is None:
        raise HTTPException(400, f"Unknown socket: {socket_key}")
    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(
        None, _tuya_set_dps, device_id, dps_num, action == "on")
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 503)


@app.get("/lock/{device_id}")
async def lock_status(device_id: str):
    _, dev_cfg, _ = _find_device(device_id)
    if dev_cfg is None:
        raise HTTPException(404, {"ok": False, "error": "Device not found"})
    loop = asyncio.get_event_loop()
    try:
        dps = await loop.run_in_executor(None, _tuya_get_dps, device_id)
        dps_num = dev_cfg.get("dps_locked", 8)
        locked = bool(dps.get(str(dps_num), dps.get(dps_num, True)))
        return {"ok": True, "locked": locked}
    except Exception as e:
        return JSONResponse({"ok": False, "locked": True, "error": str(e)}, status_code=503)


@app.post("/lock/{device_id}/{action}")
async def lock_control(device_id: str, action: str):
    if action not in ("lock", "unlock"):
        raise HTTPException(400, "action must be lock or unlock")
    _, dev_cfg, _ = _find_device(device_id)
    if dev_cfg is None:
        raise HTTPException(404, {"ok": False, "error": "Device not found"})
    dps_num = dev_cfg.get("dps_locked", 8)
    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(
        None, _tuya_set_dps, device_id, dps_num, action == "lock")
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 503)


@app.get("/alarm/{device_id}")
async def alarm_status(device_id: str):
    _, dev_cfg, _ = _find_device(device_id)
    if dev_cfg is None:
        raise HTTPException(404, {"ok": False, "error": "Device not found"})
    loop = asyncio.get_event_loop()
    try:
        dps = await loop.run_in_executor(None, _tuya_get_dps, device_id)
        dps_num = dev_cfg.get("dps_alarm", 1)
        return {"ok": True, "alarm": bool(dps.get(str(dps_num), dps.get(dps_num, False)))}
    except Exception as e:
        return JSONResponse({"ok": False, "alarm": False, "error": str(e)}, status_code=503)


@app.get("/sensor/{device_id}")
async def sensor_status(device_id: str):
    _, dev_cfg, _ = _find_device(device_id)
    if dev_cfg is None:
        raise HTTPException(404, {"ok": False, "error": "Device not found"})
    loop = asyncio.get_event_loop()
    try:
        dps = await loop.run_in_executor(None, _tuya_get_dps, device_id)
        dps_num = dev_cfg.get("dps_state", 1)
        return {"ok": True, "state": bool(dps.get(str(dps_num), dps.get(dps_num, False)))}
    except Exception as e:
        return JSONResponse({"ok": False, "state": False, "error": str(e)}, status_code=503)


@app.post("/keys")
async def update_keys(request: Request):
    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(400, "Expected a JSON array")
    loop = asyncio.get_event_loop()
    updated = await loop.run_in_executor(None, _tuya_update_keys, body)
    return {"ok": True, "updated": updated}


# ── AI ────────────────────────────────────────────────────────────────────────

@app.post("/ai/ask")
async def ai_ask(request: Request):
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(400, "text required")
    return await ask_ollama(text, body.get("context", {}))


# ── Automation triggers ───────────────────────────────────────────────────────

@app.post("/automation/calendar-refresh")
async def trigger_calendar(background_tasks: BackgroundTasks):
    background_tasks.add_task(refresh_calendar)
    return {"ok": True, "status": "triggered"}


@app.get("/automation/panel-status")
async def panel_status():
    entities = [
        "binary_sensor.guition_p4_7inch_ha_panel_online",
        "sensor.guition_p4_7inch_ha_panel_uptime",
        "sensor.guition_p4_7inch_ha_panel_wi_fi_rssi",
        "input_text.panel_calendar_events",
        "input_select.panel_theme",
    ]
    result: dict = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for eid in entities:
                r = await client.get(f"{HA_URL}/api/states/{eid}", headers=HA_HEADERS)
                if r.status_code == 200:
                    result[eid] = r.json().get("state")
    except Exception as e:
        log.error("panel status: %s", e)
    return result


# ── Panel dynamic config ─────────────────────────────────────────────────────

@app.get("/panel/config")
async def panel_config_endpoint():
    """Return panel runtime config — camera slots, feature flags.

    The ESPHome panel fetches this at boot to configure camera entity slugs
    without requiring a reflash. Edit panel_config.json and reboot the panel.
    """
    cfg = _load_panel_config()
    cfg.setdefault("features", {
        "immich":   bool(IMMICH_URL and IMMICH_KEY),
        "jellyfin": bool(JELLYFIN_URL and JELLYFIN_KEY),
        "tuya":     _TUYA_OK and bool(_tuya_config.get("wifi_devices") or _tuya_config.get("gateways")),
        "ollama":   bool(OLLAMA_URL),
        "calendar": bool(HA_URL and HA_TOKEN),
    })
    return cfg


@app.get("/panel/rooms")
async def panel_rooms():
    """Aggregate light state for each room from HA in a single API call."""
    rooms = [
        {"room": 1, "label": "Drawing Room",  "light": "switch.drawinglights"},
        {"room": 2, "label": "Office",         "light": "light.office_lights"},
        {"room": 3, "label": "Hallway",        "light": "light.hallway"},
        {"room": 4, "label": "Stairs",         "light": "light.stairs_light"},
        {"room": 5, "label": "Bedroom",        "light": "light.bedroom_lights"},
        {"room": 6, "label": "Conservatory",   "light": "switch.conservatory_switch"},
    ]
    all_states: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{HA_URL}/api/states", headers=HA_HEADERS)
            r.raise_for_status()
            all_states = {s["entity_id"]: s["state"] for s in r.json()}
    except Exception as e:
        log.error("panel/rooms: %s", e)
    return [{"room": rm["room"], "label": rm["label"],
             "light": rm["light"], "state": all_states.get(rm["light"], "unavailable")}
            for rm in rooms]


@app.get("/panel/presence")
async def panel_presence():
    """Aggregate occupancy/motion state for all presence sensors in one HA call."""
    sensors = [
        {"slot": 1,  "label": "Hallway",      "entity": "binary_sensor.hallway_motion_sensor_motion"},
        {"slot": 2,  "label": "Hallway PS",   "entity": "binary_sensor.hallway_ps_motion"},
        {"slot": 3,  "label": "Stairs",       "entity": "binary_sensor.stairs_motion_sensor_motion"},
        {"slot": 4,  "label": "Drawing",      "entity": "binary_sensor.dr_motion_sensor_motion_2"},
        {"slot": 5,  "label": "Office",       "entity": "binary_sensor.office_presence_sensor_occupancy"},
        {"slot": 6,  "label": "Toilet",       "entity": "binary_sensor.tze200_3towulqd_ts0601_motion_4"},
        {"slot": 7,  "label": "Conservatory", "entity": "binary_sensor.cps_motion"},
        {"slot": 8,  "label": "Repeater",     "entity": "binary_sensor.repeater_motion"},
        {"slot": 9,  "label": "Front Door",   "entity": "binary_sensor.front_door_motion_detected"},
        {"slot": 10, "label": "Front Bell",   "entity": "binary_sensor.front_door_bell_motion_detected"},
        {"slot": 11, "label": "Side Door",    "entity": "binary_sensor.side_door_motion_detected"},
        {"slot": 12, "label": "Garden",       "entity": "binary_sensor.garden_motion_detected"},
    ]
    all_states: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{HA_URL}/api/states", headers=HA_HEADERS)
            r.raise_for_status()
            all_states = {s["entity_id"]: s["state"] for s in r.json()}
    except Exception as e:
        log.error("panel/presence: %s", e)
    return [{"slot": s["slot"], "label": s["label"], "entity": s["entity"],
             "active": all_states.get(s["entity"], "off") == "on"}
            for s in sensors]


# ── iHost / eWeLink ──────────────────────────────────────────────────────────

def _ihost_headers() -> dict:
    return {
        "Authorization": f"Bearer {IHOST_TOKEN}",
        "Content-Type": "application/json",
    }


def _parse_ihost_device(d: dict) -> dict:
    serial = d.get("serial_number", d.get("serialNumber", ""))
    name = d.get("name", serial)
    category = d.get("display_category", "")
    state = d.get("state") or {}
    power = state.get("power", {}) or {}
    on = (power.get("powerState") == "on") if power else None
    temp = state.get("temperature", {})
    hum  = state.get("humidity", {})
    return {
        "serial": serial,
        "name": name,
        "category": category,
        "online": d.get("online", False),
        "on": on,
        "temperature": temp.get("temperature") if temp else None,
        "humidity": hum.get("humidity") if hum else None,
    }


async def _ihost_device_list() -> list:
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(
            f"{IHOST_URL}/open-api/v2/rest/devices",
            headers=_ihost_headers(),
        )
        r.raise_for_status()
        data = r.json()
    # iHost Open API v2 uses snake_case: device_list / serial_number
    raw = data.get("data", {}).get("device_list", data.get("data", {}).get("deviceList", []))
    return [_parse_ihost_device(d) for d in raw]


async def _ihost_set_power(serial: str, on: bool) -> bool:
    payload = {"power": {"powerState": "on" if on else "off"}}
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.put(
            f"{IHOST_URL}/open-api/v2/rest/devices/{serial}/state",
            headers={**_ihost_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        data = r.json()
    return data.get("error", 1) == 0


@app.get("/ihost/health")
async def ihost_health():
    if not IHOST_TOKEN:
        raise HTTPException(503, "IHOST_TOKEN not configured — run get_token.sh")
    try:
        devices = await _ihost_device_list()
        return {"ok": True, "devices": len(devices)}
    except Exception as exc:
        log.warning("ihost health: %s", exc)
        raise HTTPException(503, {"ok": False, "error": str(exc)})


@app.get("/ihost/devices")
async def ihost_devices():
    try:
        return await _ihost_device_list()
    except Exception as exc:
        log.error("ihost devices: %s", exc)
        raise HTTPException(503, {"ok": False, "error": str(exc)})


@app.get("/ihost/device/{serial}")
async def ihost_device_state(serial: str):
    try:
        devices = await _ihost_device_list()
        for d in devices:
            if d["serial"] == serial:
                return {"ok": True, **d}
        raise HTTPException(404, {"ok": False, "on": None})
    except HTTPException:
        raise
    except Exception as exc:
        log.error("ihost device %s: %s", serial, exc)
        raise HTTPException(503, {"ok": False, "error": str(exc)})


@app.post("/ihost/device/{serial}/{action}")
async def ihost_device_control(serial: str, action: str):
    if action not in ("on", "off", "toggle"):
        raise HTTPException(400, "action must be on, off, or toggle")
    if action == "toggle":
        devices = await _ihost_device_list()
        dev = next((d for d in devices if d["serial"] == serial), None)
        if dev is None:
            raise HTTPException(404, {"ok": False})
        target = not bool(dev.get("on"))
    else:
        target = (action == "on")
    ok = await _ihost_set_power(serial, target)
    return {"ok": ok, "on": target if ok else None}


@app.post("/ihost/device/{serial}")
async def ihost_device_set(serial: str, request: Request):
    """HA rest switch compat: POST body {"on": true/false}."""
    try:
        body = await request.json()
        target = bool(body.get("on", False))
    except Exception:
        raise HTTPException(400, "body must be JSON with 'on' key")
    ok = await _ihost_set_power(serial, target)
    return {"ok": ok, "on": target if ok else None}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=HUB_PORT)
