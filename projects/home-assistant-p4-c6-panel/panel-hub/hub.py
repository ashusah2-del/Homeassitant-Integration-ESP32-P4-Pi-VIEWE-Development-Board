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
from PIL import Image, ImageOps

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
# Assets under these external-library paths are excluded from the slideshow
# (case-insensitive substring match on originalPath) — e.g. the "AI Images"
# folder holds AI-upscaled/enhanced photos, not real family photos.
AI_EXCLUDE_PATHS = [p.strip().lower() for p in os.getenv("AI_EXCLUDE_PATHS", "AI Images").split(",") if p.strip()]
# Panels send only their own slideshow canvas resolution (?w=&h=) —
# nothing else panel-specific. Adding a panel of any dimension is then
# just a new ESPHome substitutions file; no backend config to update.
#
# Fit mode is universal (letterbox / contain, never crop): the WHOLE photo
# is scaled to fit inside the requested canvas, centered, with black bars
# on whichever axis is short. A landscape photo on a landscape panel fills
# top-to-bottom with thin side bars — the user picked "show the whole
# photo, fill height, no crop" over edge-to-edge cover-crop because
# cropping shaved heads/edges and looked unnatural. Same-orientation asset
# selection (fetch_random_photo) keeps the bars thin by preferring photos
# whose aspect ratio is close to the panel's.
# Hard ceiling on a panel's requested w/h — a safety net against a
# runaway request, not a per-panel setting. 1280 covers both current
# panels; bump via hub.env for a bigger future panel (bounded by ESP32
# PSRAM decode limits in practice, not an arbitrary code limit).
ABS_MAX_W = int(os.getenv("ABS_MAX_W", "1280"))
ABS_MAX_H = int(os.getenv("ABS_MAX_H", "1280"))
PHOTO_MAX_W     = int(os.getenv("MAX_W", "800"))
PHOTO_MAX_H     = int(os.getenv("MAX_H", "480"))
JPEG_QUALITY    = int(os.getenv("JPEG_QUALITY", "78"))
# Lowest JPEG quality the size-ceiling ladder will drop to at native
# resolution before it resorts to shrinking the image. A native-resolution
# photo at q=25 looks sharper on the panel than a downscaled one upscaled
# back to fill the screen, so we push quality down to this floor first.
JPEG_MIN_QUALITY = int(os.getenv("JPEG_MIN_QUALITY", "20"))
# Target size for the encoded JPEG. Must stay under ESPHome online_image's
# hard 65536-byte buffer cap; 63000 uses almost all of it (more bytes =
# sharper) while leaving a small safety margin.
JPEG_BUDGET_BYTES = int(os.getenv("JPEG_BUDGET_BYTES", "63000"))
RETRIES         = int(os.getenv("RETRIES", "8"))

HA_URL          = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN        = os.getenv("HA_TOKEN", "")
HA_HEADERS      = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

JELLYFIN_URL         = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_KEY         = os.getenv("JELLYFIN_API_KEY", "")
JELLYFIN_USER_ID     = os.getenv("JELLYFIN_USER_ID", "").strip()
JELLYFIN_PLAY_CLIENT = os.getenv("JELLYFIN_PLAY_CLIENT", "").strip()
# Browser-reachable Jellyfin base (for the "Watch on Jellyfin" link on the /yts
# browser page) — NOT the docker-gateway URL the hub uses internally.
JELLYFIN_PUBLIC_URL  = os.getenv("JELLYFIN_PUBLIC_URL", "http://192.168.55.59:8096").rstrip("/")
FIRETV_ADB_CONTAINER = os.getenv("FIRETV_ADB_CONTAINER", "adb-server")
FIRETV_ADB_DEVICE    = os.getenv("FIRETV_ADB_DEVICE", "192.168.55.77:5555")
POSTER_MAX_W    = int(os.getenv("POSTER_MAX_W", "280"))
POSTER_MAX_H    = int(os.getenv("POSTER_MAX_H", "400"))
POSTER_QUALITY  = int(os.getenv("POSTER_QUALITY", "85"))

# ── YTS browse + Radarr grab (Latest Movies page) ──────────────────────────────
# The panel browses the YTS "latest movies" feed and, on Download, hands the
# movie to Radarr by IMDb id. Radarr drives Prowlarr → qBittorrent → import into
# the Jellyfin library (that is what later flips a movie's badge to "library").
YTS_URL                   = os.getenv("YTS_URL", "https://movies-api.accel.li").rstrip("/")
RADARR_URL                = os.getenv("RADARR_URL", "http://172.17.0.1:7878").rstrip("/")
RADARR_API_KEY            = os.getenv("RADARR_API_KEY", "")
RADARR_QUALITY_PROFILE_ID = int(os.getenv("RADARR_QUALITY_PROFILE_ID", "4"))  # 4 = HD-1080p
RADARR_ROOT_FOLDER        = os.getenv("RADARR_ROOT_FOLDER", "/data/Movies")
CROSSREF_TTL              = int(os.getenv("CROSSREF_TTL", "60"))  # seconds
# qBittorrent — fallback for the rare movie Radarr/TMDb can't match (add the YTS
# torrent straight to qBittorrent). host-local → docker gateway.
QBIT_URL                  = os.getenv("QBIT_URL", "http://172.17.0.1:8080").rstrip("/")
QBIT_USER                 = os.getenv("QBIT_USER", "")
QBIT_PASS                 = os.getenv("QBIT_PASS", "")
QBIT_QUALITY              = os.getenv("QBIT_QUALITY", "1080p")  # preferred YTS quality
# YTS magnet trackers (standard public set the YTS site itself uses).
_YTS_TRACKERS = [
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:80",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://glotorrents.pw:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://torrent.gresille.org:80/announce",
    "udp://p4p.arenabg.com:1337",
    "udp://tracker.leechers-paradise.org:6969",
]

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
                quality: int = JPEG_QUALITY, subsampling: int = 2,
                fill: bool = False, budget_bytes: int = JPEG_BUDGET_BYTES) -> bytes:
    """Re-encode to SOF0 baseline JPEG on a 16-pixel-aligned canvas.

    fill=False (default) fits the photo within the canvas and letterboxes
    the shortfall. fill=True instead scales to cover the whole canvas and
    center-crops the overflow (no black bars).

    Guarantees the returned JPEG is <= budget_bytes. ESPHome's online_image
    component hard-caps its download buffer at 65536 bytes (a schema limit,
    not a tunable default — cv.int_range(256, 65536)), so an oversized
    response makes the panel silently fall back to its stock image on every
    fetch.

    Sharpness strategy: the panel decodes at native canvas resolution, so a
    full-resolution image at low JPEG quality looks sharper than a shrunk
    one upscaled back to fill the screen. We therefore drop quality all the
    way to JPEG_MIN_QUALITY at native resolution FIRST, and only shrink the
    geometry if even that overflows the budget. `optimize=True` squeezes
    ~5-10% more out of the Huffman tables at identical visual quality (still
    baseline SOF0, JPEGDEC-safe), so more detail fits under the cap.
    """
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    cw = max(16, (max_w // 16) * 16)
    ch = max(16, (max_h // 16) * 16)

    def _compose(w: int, h: int) -> Image.Image:
        if fill:
            scale = max(w / img.width, h / img.height)
            resized = img.resize((round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS)
            left = (resized.width - w) // 2
            top = (resized.height - h) // 2
            return resized.crop((left, top, left + w, top + h))
        thumb = img.copy()
        thumb.thumbnail((w, h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(thumb, ((w - thumb.width) // 2, (h - thumb.height) // 2))
        return canvas

    def _save(canvas: Image.Image, q: int) -> bytes:
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=q,
                    optimize=True, progressive=False, subsampling=subsampling)
        return buf.getvalue()

    def _fit_quality(canvas: Image.Image) -> tuple[bytes, int]:
        # Highest quality (stepping down by 5) whose encode fits the budget,
        # bottoming out at JPEG_MIN_QUALITY. Fine step lands close to the
        # cap so we spend the whole budget on detail.
        q = quality
        data = _save(canvas, q)
        while len(data) > budget_bytes and q > JPEG_MIN_QUALITY:
            q = max(JPEG_MIN_QUALITY, q - 5)
            data = _save(canvas, q)
        return data, q

    w, h = cw, ch
    canvas = _compose(w, h)
    data, q = _fit_quality(canvas)

    # Only if native resolution at the quality floor STILL overflows do we
    # shrink geometry — kept as a last resort because upscaling the result
    # on the panel is what makes photos look blurry. Shrink in gentle 8%
    # steps (not a big jump) so a photo that only slightly overflows lands
    # just under native (e.g. 1184x736, a barely-visible 1.08x upscale)
    # rather than being over-shrunk to a soft 912x560.
    while len(data) > budget_bytes and w > 128 and h > 128:
        w = max(16, (max(128, int(w * 0.92)) // 16) * 16)
        h = max(16, (max(128, int(h * 0.92)) // 16) * 16)
        canvas = _compose(w, h)
        data, q = _fit_quality(canvas)

    if len(data) > budget_bytes:
        log.warning("encode_sof0: could not fit under %d bytes (got %d at %dx%d q=%d)",
                    budget_bytes, len(data), w, h, q)
    return data


# ── Immich ────────────────────────────────────────────────────────────────────

def _is_ai_generated(asset: dict) -> bool:
    path = (asset.get("originalPath") or "").lower()
    return any(kw in path for kw in AI_EXCLUDE_PATHS)


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


async def fetch_random_photo(target_w: int = PHOTO_MAX_W, target_h: int = PHOTO_MAX_H) -> bytes | None:
    # Fit the WHOLE photo inside the canvas (letterbox, fill=False) — never
    # crop. A landscape photo on the landscape panel fills top-to-bottom
    # with thin black side bars; nothing is cut off, which reads as more
    # natural than a cover-crop that shaves heads/edges. (See the module
    # comment above PHOTO_MAX_W.) This is the one caller of encode_sof0's
    # fill=, so the choice is hardcoded here.
    #
    # Match the asset's orientation to the requested canvas — a portrait
    # photo letterboxed into a landscape canvas ends up tiny with huge side
    # bars, so we prefer (and retry for) same-orientation photos below.
    want_landscape = target_w >= target_h
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
            images = [a for a in images if not _is_ai_generated(a)]
            if not images:
                log.warning("attempt %d: no non-AI IMAGE assets in batch", attempt)
                continue

            if LANDSCAPE_PREFER:
                if want_landscape:
                    matching = [a for a in images if (a.get("width") or 0) >= (a.get("height") or 0)]
                else:
                    matching = [a for a in images if (a.get("height") or 0) > (a.get("width") or 0)]
                # Re-fetch a fresh batch rather than immediately settling
                # for the wrong orientation — a portrait photo cover-cropped
                # onto a landscape canvas (or vice versa) loses most of its
                # frame. Only fall back to any orientation on the very last
                # attempt, so a photo still gets served eventually.
                if not matching and attempt < RETRIES - 1:
                    log.info("attempt %d: no %s assets in batch, retrying for a better match",
                             attempt, "landscape" if want_landscape else "portrait")
                    continue
                pool = matching or images
            else:
                pool = images
            # Among the pool, prefer whichever asset's own aspect ratio is
            # closest to the requested canvas's. Under fill=True this
            # minimizes how much gets cropped off (a well-matched photo
            # loses only a sliver off one edge instead of a large chunk);
            # under fill=False it minimizes the letterbox bars for the
            # same reason. Free improvement either way.
            target_aspect = target_w / target_h
            item = min(pool, key=lambda a: abs((a.get("width") or 1) / (a.get("height") or 1) - target_aspect))

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
                None, encode_sof0, raw, target_w, target_h, JPEG_QUALITY, 2, False)

            orient = "landscape" if (item.get("width", 0) >= item.get("height", 0)) else "portrait-fallback"
            scope = f"person:{person_name}" if person_name else ("favorites" if FAVORITES_ONLY else "all")
            log.info("photo %s (%s, %s) SOF 0xFF%02X: %d→%d bytes (<=%dx%d)",
                     item["id"], scope, orient, sof, len(raw), len(jpeg), target_w, target_h)
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


# ── YTS browse + Radarr grab ────────────────────────────────────────────────────

# yts_id (str) -> medium_cover_image URL, populated by fetch_yts_movies so the
# poster endpoint can re-encode without re-querying the list.
_yts_poster_url: dict[str, str] = {}
# yts_id (str) -> imdb_code, so /yts/download can resolve without a refetch.
_yts_imdb: dict[str, str] = {}
# yts_id (str) -> {title, year, torrents:[{quality, hash}]} for the qBittorrent fallback.
_yts_meta: dict[str, dict] = {}

# Cross-reference caches (what is already in Jellyfin / Radarr), refreshed lazily.
_crossref_lock = asyncio.Lock()
_crossref_ts: float = 0.0
_jf_imdb_set: set[str] = set()                 # imdb codes present in Jellyfin
_jf_item_by_imdb: dict[str, str] = {}          # imdb -> Jellyfin itemId (for watch links)
_radarr_by_imdb: dict[str, dict] = {}          # imdb -> {"id", "hasFile"}
_radarr_queue_ids: set[int] = set()            # radarr movie ids currently downloading


def _radarr_headers() -> dict[str, str]:
    return {"X-Api-Key": RADARR_API_KEY, "Accept": "application/json"}


async def _refresh_crossref() -> None:
    """Rebuild the Jellyfin/Radarr lookup caches (best-effort, ~CROSSREF_TTL)."""
    global _crossref_ts, _jf_imdb_set, _jf_item_by_imdb, _radarr_by_imdb, _radarr_queue_ids
    async with _crossref_lock:
        import time as _time
        if _time.time() - _crossref_ts < CROSSREF_TTL:
            return
        # Jellyfin: set of IMDb ids in the library + imdb -> itemId for watch links.
        jf: set[str] = set()
        jf_items: dict[str, str] = {}
        try:
            uid = await _get_jf_user_id()
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{JELLYFIN_URL}/Users/{uid}/Items",
                    headers={"X-Emby-Token": JELLYFIN_KEY},
                    params={"Recursive": "true", "IncludeItemTypes": "Movie",
                            "Fields": "ProviderIds", "Limit": 100000})
                r.raise_for_status()
                for item in r.json().get("Items", []):
                    imdb = (item.get("ProviderIds") or {}).get("Imdb", "")
                    if imdb:
                        code = imdb.strip().lower()
                        jf.add(code)
                        if item.get("Id"):
                            jf_items[code] = item["Id"]
        except Exception as e:
            log.warning("crossref: Jellyfin library scan failed: %s", e)
        # Radarr: imdb -> {id, hasFile} and the set of downloading movie ids.
        rmap: dict[str, dict] = {}
        queue: set[int] = set()
        if RADARR_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(f"{RADARR_URL}/api/v3/movie", headers=_radarr_headers())
                    r.raise_for_status()
                    for m in r.json():
                        code = (m.get("imdbId") or "").strip().lower()
                        if code:
                            rmap[code] = {"id": m.get("id"), "hasFile": bool(m.get("hasFile"))}
                    rq = await client.get(f"{RADARR_URL}/api/v3/queue",
                                          headers=_radarr_headers(),
                                          params={"pageSize": 1000})
                    if rq.is_success:
                        for rec in rq.json().get("records", []):
                            if rec.get("movieId"):
                                queue.add(int(rec["movieId"]))
            except Exception as e:
                log.warning("crossref: Radarr scan failed: %s", e)
        _jf_imdb_set, _jf_item_by_imdb, _radarr_by_imdb, _radarr_queue_ids = jf, jf_items, rmap, queue
        _crossref_ts = _time.time()
        log.info("crossref refreshed: jellyfin=%d radarr=%d queue=%d",
                 len(jf), len(rmap), len(queue))


def _movie_status(imdb: str) -> str:
    code = (imdb or "").strip().lower()
    if not code:
        return "available"
    rad = _radarr_by_imdb.get(code)
    if code in _jf_imdb_set or (rad and rad.get("hasFile")):
        return "library"
    if rad and rad.get("id") in _radarr_queue_ids:
        return "downloading"
    return "available"


def _watch_url(imdb: str, title: str) -> str:
    """Browser link to watch an in-library movie on Jellyfin.

    Exact details page when we know the Jellyfin itemId; otherwise a title
    search (e.g. when the Jellyfin API key is unavailable).
    """
    item_id = _jf_item_by_imdb.get((imdb or "").strip().lower())
    if item_id:
        return f"{JELLYFIN_PUBLIC_URL}/web/#/details?id={item_id}"
    return f"{JELLYFIN_PUBLIC_URL}/web/#/search.html?query={urllib.parse.quote(title or '')}"


async def fetch_yts_movies(page: int, sort: str = "date_added", query: str = "") -> dict:
    """YTS movies + a library/downloading/available status.

    sort: "date_added" (newest added to YTS first) or "year" (newest release first).
    query: optional free-text title search.
    """
    sort_by = "year" if sort == "year" else "date_added"
    await _refresh_crossref()
    params = {"page": max(1, page), "limit": 8, "sort_by": sort_by, "order_by": "desc"}
    if query.strip():
        params["query_term"] = query.strip()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(f"{YTS_URL}/api/v2/list_movies.json", params=params)
        r.raise_for_status()
        data = r.json().get("data", {})
    movies = []
    for m in data.get("movies", []) or []:
        yid = str(m.get("id", ""))
        imdb = (m.get("imdb_code") or "").strip()
        if yid:
            _yts_poster_url[yid] = m.get("medium_cover_image") or m.get("small_cover_image") or ""
            _yts_imdb[yid] = imdb
            _yts_meta[yid] = {
                "title": (m.get("title_english") or m.get("title") or "").strip(),
                "year": m.get("year") or 0,
                "torrents": [{"quality": t.get("quality", ""), "hash": t.get("hash", "")}
                             for t in (m.get("torrents") or []) if t.get("hash")],
            }
        title = (m.get("title_english") or m.get("title") or "Unknown").strip()
        status = _movie_status(imdb)
        movies.append({
            "id": yid,
            "title": title,
            "year": m.get("year") or 0,
            "rating": m.get("rating") or 0,
            "imdb": imdb,
            "status": status,
            "watch_url": _watch_url(imdb, title) if status == "library" else "",
        })
    return {
        "page": max(1, page),
        "movie_count": int(data.get("movie_count", 0)),
        "movies": movies,
    }


async def _yts_cover_url(yts_id: str) -> str:
    url = _yts_poster_url.get(yts_id)
    if url:
        return url
    # Fallback: ask YTS directly (cache miss after a restart or deep link).
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(f"{YTS_URL}/api/v2/movie_details.json",
                                 params={"movie_id": yts_id})
            r.raise_for_status()
            mv = r.json().get("data", {}).get("movie", {})
            url = mv.get("medium_cover_image") or mv.get("large_cover_image") or ""
            if url:
                _yts_poster_url[yts_id] = url
                _yts_imdb[yts_id] = (mv.get("imdb_code") or "").strip()
                _yts_meta[yts_id] = {
                    "title": (mv.get("title_english") or mv.get("title") or "").strip(),
                    "year": mv.get("year") or 0,
                    "torrents": [{"quality": t.get("quality", ""), "hash": t.get("hash", "")}
                                 for t in (mv.get("torrents") or []) if t.get("hash")],
                }
    except Exception as e:
        log.warning("yts cover lookup failed for %s: %s", yts_id, e)
    return url or ""


async def fetch_yts_poster(yts_id: str) -> bytes | None:
    url = await _yts_cover_url(yts_id)
    if not url:
        return None
    loop = asyncio.get_event_loop()
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            if r.content:
                return await loop.run_in_executor(
                    None, encode_sof0, r.content, POSTER_MAX_W, POSTER_MAX_H,
                    POSTER_QUALITY, 2)
    except Exception as e:
        log.warning("yts poster fetch failed for %s: %s", yts_id, e)
    return None


async def _yts_imdb_code(yts_id: str) -> str:
    code = _yts_imdb.get(yts_id)
    if code:
        return code
    await _yts_cover_url(yts_id)  # side-effect: also fills _yts_imdb
    return _yts_imdb.get(yts_id, "")


async def qbit_add(yts_id: str) -> dict:
    """Fallback: add the movie's YTS torrent straight to qBittorrent.

    Used when Radarr/TMDb can't match the title. Builds a magnet from the
    preferred-quality torrent hash and hands it to the qBittorrent WebUI.
    """
    meta = _yts_meta.get(yts_id)
    if not meta:
        await _yts_cover_url(yts_id)  # side-effect: populates _yts_meta
        meta = _yts_meta.get(yts_id)
    torrents = (meta or {}).get("torrents") or []
    if not torrents:
        return {"ok": False, "status": "error", "message": "no torrent available for movie"}
    best = next((t for t in torrents if t["quality"] == QBIT_QUALITY), torrents[0])
    name = f"{meta.get('title', '')} ({meta.get('year', '')}) [{best['quality']}]".strip()
    trackers = "".join(f"&tr={urllib.parse.quote(tr)}" for tr in _YTS_TRACKERS)
    magnet = f"magnet:?xt=urn:btih:{best['hash']}&dn={urllib.parse.quote(name)}{trackers}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            if QBIT_USER:
                lr = await client.post(f"{QBIT_URL}/api/v2/auth/login",
                                       data={"username": QBIT_USER, "password": QBIT_PASS},
                                       headers={"Referer": QBIT_URL})
                lr.raise_for_status()
            ar = await client.post(f"{QBIT_URL}/api/v2/torrents/add",
                                   data={"urls": magnet}, headers={"Referer": QBIT_URL})
            if not ar.is_success:
                log.error("qBittorrent add %d: %s", ar.status_code, ar.text)
                return {"ok": False, "status": "error", "message": f"qbittorrent add failed ({ar.status_code})"}
    except Exception as e:
        log.error("qBittorrent add failed: %s", e)
        return {"ok": False, "status": "error", "message": str(e)}
    log.info("qBittorrent: added %s (%s) via magnet", yts_id, best["quality"])
    return {"ok": True, "status": "qbittorrent"}


async def radarr_add(yts_id: str) -> dict:
    """Add the movie to Radarr by IMDb id and trigger a search.

    Returns {ok, status} where status is library|downloading|exists|queued, or
    falls back to qBittorrent (status "qbittorrent") when Radarr can't match it.
    """
    if not RADARR_API_KEY:
        return await qbit_add(yts_id)
    imdb = await _yts_imdb_code(yts_id)
    if not imdb:
        return await qbit_add(yts_id)
    async with httpx.AsyncClient(timeout=20) as client:
        lr = await client.get(f"{RADARR_URL}/api/v3/movie/lookup",
                              headers=_radarr_headers(), params={"term": f"imdb:{imdb}"})
        lr.raise_for_status()
        found = lr.json()
        movie = found[0] if isinstance(found, list) and found else (found if isinstance(found, dict) else None)
        if not movie or not movie.get("tmdbId"):
            # Radarr/TMDb can't match it — hand the YTS torrent to qBittorrent.
            return await qbit_add(yts_id)

        existing_id = movie.get("id") or 0
        if existing_id:
            # Already tracked by Radarr. If the file is present it's effectively
            # in the library; otherwise (re)trigger a search.
            if movie.get("hasFile"):
                return {"ok": True, "status": "library"}
            await client.post(f"{RADARR_URL}/api/v3/command", headers=_radarr_headers(),
                              json={"name": "MoviesSearch", "movieIds": [existing_id]})
            return {"ok": True, "status": "exists"}

        # Not tracked yet — add it (monitored) and search immediately.
        payload = dict(movie)
        payload.update({
            "qualityProfileId": RADARR_QUALITY_PROFILE_ID,
            "rootFolderPath": RADARR_ROOT_FOLDER,
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": True},
        })
        ar = await client.post(f"{RADARR_URL}/api/v3/movie", headers=_radarr_headers(), json=payload)
        if not ar.is_success:
            log.error("Radarr add %d: %s", ar.status_code, ar.text)
            return {"ok": False, "status": "error", "message": f"radarr add failed ({ar.status_code})"}
    # Force the next browse refresh to reflect the new queue state.
    global _crossref_ts
    _crossref_ts = 0.0
    log.info("Radarr: added %s (imdb %s), search triggered", yts_id, imdb)
    return {"ok": True, "status": "queued"}


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
        # The gateway addresses Zigbee sub-devices by their node id ("cid",
        # e.g. a4c1387dddd2bba8), NOT the Tuya cloud device id. Without an
        # explicit cid tinytuya falls back to dev_id and the gateway drops
        # the request ("No response"). Get each cid from
        # gw.subdev_query() or the Tuya IoT platform device page.
        d = tinytuya.Device(
            dev_id=dev_cfg["id"], address=gw_cfg["ip"],
            local_key=gw_cfg["local_key"],
            version=float(gw_cfg.get("version", "3.4")),
            cid=dev_cfg.get("cid") or dev_cfg["id"],
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

            events: list[tuple[datetime, str]] = []
            for cal_id in cal_ids:
                r2 = await client.get(
                    f"{HA_URL}/api/calendars/{cal_id}",
                    headers=HA_HEADERS,
                    params={"start": now.isoformat(),
                            "end": week_end.isoformat()})
                if r2.status_code != 200:
                    continue
                for evt in r2.json():
                    start = evt.get("start", {})
                    dt_str = start.get("dateTime") or start.get("date", "")
                    summary = evt.get("summary", "")
                    if "T" in dt_str:
                        dt = datetime.fromisoformat(dt_str)
                        line = f"{dt.strftime('%a %d %H:%M')} {summary}"
                    else:
                        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
                        line = f"{dt.strftime('%a %d')} All Day: {summary}"
                    events.append((dt, line[:40]))

            events.sort(key=lambda x: x[0])
            lines = [line for _, line in events]
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
async def random_photo(
    # A new panel of any dimension just sends its own w/h here — bump
    # ABS_MAX_W/ABS_MAX_H in hub.env if it's bigger than the current
    # ceiling (still bounded by ESP32 PSRAM decode limits, not arbitrary).
    # The panel sends only its own screen size; fill mode is universal
    # (see comment on fetch_random_photo) — no `fill` param here.
    w: int = Query(PHOTO_MAX_W, ge=16, le=ABS_MAX_W),
    h: int = Query(PHOTO_MAX_H, ge=16, le=ABS_MAX_H),
):
    jpeg = await fetch_random_photo(w, h)
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


# ── YTS browse + Radarr grab (Latest Movies page) ──────────────────────────────

_YTS_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Latest Movies</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0a1017; color: #e8eef5;
         font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  header { position: sticky; top: 0; z-index: 5; display: flex; align-items: center;
           gap: 16px; padding: 14px 22px; background: #0e1824; border-bottom: 1px solid #1e2c3c; }
  header h1 { font-size: 20px; margin: 0; font-weight: 600; }
  header .sp { flex: 1; }
  button { font: inherit; cursor: pointer; border: 0; border-radius: 8px; color: #fff;
           background: #27405c; padding: 8px 14px; }
  button:hover { background: #335a7f; }
  button:disabled { opacity: .4; cursor: default; }
  #grid { display: grid; gap: 18px; padding: 22px;
          grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); }
  .card { background: #111c28; border: 1px solid #1e2c3c; border-radius: 12px; overflow: hidden;
          display: flex; flex-direction: column; }
  .card img { width: 100%; aspect-ratio: 2/3; object-fit: cover; background: #000; display: block; }
  .card .body { padding: 10px 11px 12px; display: flex; flex-direction: column; gap: 8px; flex: 1; }
  .card .title { font-size: 14px; font-weight: 600; line-height: 1.25; }
  .card .meta { font-size: 12px; color: #8aa0b6; display: flex; align-items: center; gap: 8px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
  .s-library { background: #35c46a; } .s-downloading { background: #e8b23a; }
  .s-available { background: #5c7488; }
  .grab, .watch, .dling { margin-top: auto; }
  .grab { background: #1f6d3c; }
  .grab:hover { background: #268048; }
  .grab.done { background: #27405c; }
  .watch { background: #6b3fb0; }
  .watch:hover { background: #7d4ec9; }
  .dling { background: #7a5a16; }
  footer { display: flex; justify-content: center; align-items: center; gap: 16px; padding: 10px 0 30px; }
  .toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
           background: #1b2a3a; border: 1px solid #2b4258; padding: 10px 18px; border-radius: 10px;
           opacity: 0; transition: opacity .2s; pointer-events: none; }
  .toast.show { opacity: 1; }
  .search { position: relative; flex: 1; max-width: 420px; }
  .search input { width: 100%; padding: 9px 12px; border-radius: 8px; border: 1px solid #27405c;
                  background: #0a131c; color: #e8eef5; font: inherit; }
  .search input:focus { outline: none; border-color: #4a76a8; }
  .suggest { position: absolute; left: 0; right: 0; top: 44px; background: #0e1824;
             border: 1px solid #27405c; border-radius: 8px; overflow: hidden; z-index: 10; display: none; }
  .suggest.open { display: block; }
  .suggest div { padding: 9px 12px; cursor: pointer; font-size: 14px; }
  .suggest div:hover, .suggest div.active { background: #1b2f45; }
  select { font: inherit; background: #27405c; color: #fff; border: 0; border-radius: 8px; padding: 8px 10px; }
</style></head><body>
<header>
  <h1>Latest Movies</h1>
  <div class="search">
    <input id="q" type="text" placeholder="Search movies…" autocomplete="off">
    <div id="suggest" class="suggest"></div>
  </div>
  <select id="sort" title="Sort order">
    <option value="date_added">Latest added</option>
    <option value="year">Release date</option>
  </select>
  <span class="sp"></span>
  <button id="prev">&larr; Prev</button>
  <span id="pagelbl">--</span>
  <button id="next">Next &rarr;</button>
</header>
<div id="grid"></div>
<footer>
  <span style="color:#8aa0b6;font-size:13px">
    <span class="dot s-library"></span> In library &nbsp;
    <span class="dot s-downloading"></span> Downloading &nbsp;
    <span class="dot s-available"></span> Available
  </span>
</footer>
<div class="toast" id="toast"></div>
<script>
let page = 1, total = 0, sort = 'date_added', query = '';
const grid = document.getElementById('grid');
const toast = document.getElementById('toast');
const qEl = document.getElementById('q');
const suggEl = document.getElementById('suggest');
function flash(msg) { toast.textContent = msg; toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2500); }
async function load() {
  grid.innerHTML = '<p style="color:#8aa0b6;padding:10px">Loading…</p>';
  try {
    const r = await fetch('/yts/movies?page=' + page + '&sort=' + sort +
                          '&query=' + encodeURIComponent(query));
    const d = await r.json();
    total = d.movie_count || 0;
    render(d.movies || []);
  } catch (e) { grid.innerHTML = '<p style="color:#e86a6a;padding:10px">Hub error: ' + e + '</p>'; }
  document.getElementById('pagelbl').textContent =
    total ? (((page-1)*8)+1) + '–' + Math.min(page*8, total) + ' of ' + total : '--';
  document.getElementById('prev').disabled = page <= 1;
  document.getElementById('next').disabled = page*8 >= total;
}
function render(movies) {
  grid.innerHTML = '';
  for (const m of movies) {
    const card = document.createElement('div');
    card.className = 'card';
    let btnHtml;
    if (m.status === 'library')       btnHtml = '<button class="watch">Watch &#9654;</button>';
    else if (m.status === 'downloading') btnHtml = '<button class="dling" disabled>Downloading…</button>';
    else                              btnHtml = '<button class="grab" data-id="' + m.id + '">Download</button>';
    const posterClick = (m.status === 'library' && m.watch_url) ? ' style="cursor:pointer"' : '';
    card.innerHTML =
      '<img loading="lazy" src="/yts/poster/' + m.id + '" alt=""' + posterClick + '>' +
      '<div class="body">' +
        '<div class="title">' + esc(m.title) + '</div>' +
        '<div class="meta"><span class="dot s-' + m.status + '"></span>' +
          (m.year || '') + (m.rating ? ' · ★ ' + m.rating : '') + '</div>' +
        btnHtml +
      '</div>';
    if (m.status === 'library' && m.watch_url) {
      const open = () => window.open(m.watch_url, '_blank', 'noopener');
      card.querySelector('.watch').addEventListener('click', open);
      card.querySelector('img').addEventListener('click', open);
    } else if (m.status === 'available') {
      card.querySelector('.grab').addEventListener('click', (e) => grab(e.target));
    }
    grid.appendChild(card);
  }
}
async function grab(btn) {
  btn.disabled = true; btn.textContent = 'Adding…';
  try {
    const r = await fetch('/yts/download/' + btn.dataset.id, { method: 'POST' });
    const d = await r.json();
    const map = { queued:'Queued in Radarr', exists:'Search triggered', library:'Already in library',
                  qbittorrent:'Sent to qBittorrent' };
    const label = map[d.status] || (d.ok ? 'Done' : 'Failed');
    btn.textContent = d.ok ? label : 'Failed'; btn.classList.add('done');
    flash(label);
  } catch (e) { btn.textContent = 'Failed'; btn.disabled = false; flash('Request failed'); }
}
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
document.getElementById('prev').onclick = () => { if (page > 1) { page--; load(); } };
document.getElementById('next').onclick = () => { if (page*8 < total) { page++; load(); } };
document.getElementById('sort').onchange = (e) => { sort = e.target.value; page = 1; load(); };

// ── Search + autosuggest ──
let suggTimer = null, suggItems = [], suggActive = -1;
function closeSugg() { suggEl.classList.remove('open'); suggActive = -1; }
function runSearch(term) { query = term.trim(); page = 1; closeSugg(); load(); }
function renderSugg() {
  if (!suggItems.length) { closeSugg(); return; }
  suggEl.innerHTML = suggItems.map((s, i) =>
    '<div data-i="' + i + '"' + (i === suggActive ? ' class="active"' : '') + '>' +
      esc(s.title) + (s.year ? ' (' + s.year + ')' : '') + '</div>').join('');
  suggEl.classList.add('open');
  suggEl.querySelectorAll('div').forEach(d =>
    d.onclick = () => { qEl.value = suggItems[+d.dataset.i].title; runSearch(qEl.value); });
}
qEl.addEventListener('input', () => {
  clearTimeout(suggTimer);
  const term = qEl.value.trim();
  if (!term) { closeSugg(); return; }
  suggTimer = setTimeout(async () => {
    try {
      const r = await fetch('/yts/suggest?q=' + encodeURIComponent(term));
      suggItems = (await r.json()).suggestions || []; suggActive = -1; renderSugg();
    } catch (e) { closeSugg(); }
  }, 250);
});
qEl.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown') { e.preventDefault(); if (suggItems.length) { suggActive = (suggActive+1) % suggItems.length; renderSugg(); } }
  else if (e.key === 'ArrowUp') { e.preventDefault(); if (suggItems.length) { suggActive = (suggActive-1+suggItems.length) % suggItems.length; renderSugg(); } }
  else if (e.key === 'Enter') { if (suggActive >= 0 && suggItems[suggActive]) qEl.value = suggItems[suggActive].title; runSearch(qEl.value); }
  else if (e.key === 'Escape') { closeSugg(); }
});
document.addEventListener('click', (e) => { if (!e.target.closest('.search')) closeSugg(); });
load();
</script></body></html>"""


@app.get("/yts")
async def yts_ui():
    return Response(content=_YTS_HTML, media_type="text/html")


@app.get("/yts/movies")
async def yts_movies(page: int = Query(1, ge=1),
                     sort: str = Query("date_added"),
                     query: str = Query("")):
    try:
        return await fetch_yts_movies(page, sort, query)
    except Exception as e:
        raise HTTPException(503, str(e))


@app.get("/yts/suggest")
async def yts_suggest(q: str = Query("")):
    """Lightweight title autosuggest for the browser search box."""
    if not q.strip():
        return {"suggestions": []}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(f"{YTS_URL}/api/v2/list_movies.json",
                                 params={"query_term": q.strip(), "limit": 6,
                                         "sort_by": "download_count", "order_by": "desc"})
            r.raise_for_status()
            movies = r.json().get("data", {}).get("movies", []) or []
        return {"suggestions": [
            {"id": str(m.get("id", "")),
             "title": (m.get("title_english") or m.get("title") or "").strip(),
             "year": m.get("year") or 0}
            for m in movies]}
    except Exception as e:
        log.warning("yts suggest failed for %r: %s", q, e)
        return {"suggestions": []}


@app.get("/yts/poster/{yts_id}")
async def yts_poster(yts_id: str):
    jpeg = await fetch_yts_poster(yts_id)
    if not jpeg:
        raise HTTPException(404, "Poster not found")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=86400"})


@app.post("/yts/download/{yts_id}")
async def yts_download(yts_id: str):
    try:
        return await radarr_add(yts_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


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
    payload = {"state": {"power": {"powerState": "on" if on else "off"}}}
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.put(
            f"{IHOST_URL}/open-api/v2/rest/devices/{serial}",
            headers={**_ihost_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if not r.content:
            return True
        return r.json().get("error", 1) == 0


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
