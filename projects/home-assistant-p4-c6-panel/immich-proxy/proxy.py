#!/usr/bin/env python3
"""
Immich SOF-filter proxy for ESP32-P4 panel.

Fetches a random IMAGE preview from Immich.  If it's already SOF0 (baseline
sequential) it is passed through unmodified — exactly what worked before.
If it's SOF1 (extended sequential) it is re-encoded with Pillow as SOF0.
SOF1 crashes JPEGDEC 1.2.7 without returning a recoverable error.

Endpoint: GET /random-photo  → SOF0 baseline JPEG
"""

import io
import json
import logging
import os
import random
import struct
import urllib.request
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from PIL import Image, ImageOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("immich-proxy")

IMMICH_URL   = os.environ["IMMICH_URL"].rstrip("/")
IMMICH_KEY   = os.environ["IMMICH_API_KEY"]
PROXY_PORT   = int(os.environ.get("PROXY_PORT", "8765"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "85"))
RETRIES      = int(os.environ.get("RETRIES", "8"))
FAVORITES_ONLY = os.environ.get("FAVORITES_ONLY", "false").lower() in ("1", "true", "yes", "on")
PERSON_NAMES = [p.strip() for p in os.environ.get("IMMICH_PERSON_NAMES", "").split(",") if p.strip()]
# Home Assistant — only needed for the /camera/<name> endpoint.
HA_URL       = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN     = os.environ.get("HA_TOKEN", "")
# Cap output dimensions — the ESP32-P4 panel decodes to RGB565 in PSRAM and
# decoding a 1440x1920 JPEG needs ~5.5MB working memory which causes crashes.
# 800x480 is a safe 16-pixel-aligned canvas for JPEGDEC on the ESP32 panel.
MAX_W           = int(os.environ.get("MAX_W", "800"))
MAX_H           = int(os.environ.get("MAX_H", "480"))
# /random-photo accepts optional ?w=&h= so different panels (different
# screen sizes) can each get a canvas sized for their own slideshow area.
# Clamped so a bogus request can't ask for a canvas too large for the
# ESP32's PSRAM to decode (see MAX_W/MAX_H comment above).
ABS_MAX_W       = int(os.environ.get("ABS_MAX_W", "1280"))
ABS_MAX_H       = int(os.environ.get("ABS_MAX_H", "800"))
# Fetch N candidates per request and prefer landscape (width >= height).
# Falls back to any orientation when no landscape exists in the batch.
LANDSCAPE_PREFER = os.environ.get("LANDSCAPE_PREFER", "true").lower() in ("1", "true", "yes", "on")
PORTRAIT_BATCH   = int(os.environ.get("PORTRAIT_BATCH", "4"))
# Assets under these external-library paths are excluded from the slideshow
# (case-insensitive substring match on originalPath) — e.g. the "AI Images"
# folder holds AI-upscaled/enhanced photos, not real family photos.
AI_EXCLUDE_PATHS = [p.strip().lower() for p in os.environ.get("AI_EXCLUDE_PATHS", "AI Images").split(",") if p.strip()]
_PERSON_IDS: list[tuple[str, str]] | None = None


def _is_ai_generated(asset: dict) -> bool:
    path = (asset.get("originalPath") or "").lower()
    return any(kw in path for kw in AI_EXCLUDE_PATHS)


def _get(url: str, headers: dict, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str, headers: dict, timeout: int = 30):
    return json.loads(_get(url, headers, timeout).decode("utf-8"))


def _resolve_person_ids() -> list[tuple[str, str]]:
    """Resolve configured Immich people names to IDs.

    Returns (display_name, id) pairs. Each slideshow request picks one pair,
    which gives OR semantics across configured people names.
    """
    global _PERSON_IDS
    if _PERSON_IDS is not None:
        return _PERSON_IDS
    if not PERSON_NAMES:
        _PERSON_IDS = []
        return _PERSON_IDS

    try:
        data = _get_json(f"{IMMICH_URL}/api/people", {"x-api-key": IMMICH_KEY, "Accept": "application/json"})
        people = data.get("people", data) if isinstance(data, dict) else data
        if not isinstance(people, list):
            raise RuntimeError("unexpected people response")

        resolved: list[tuple[str, str]] = []
        for wanted in PERSON_NAMES:
            wanted_l = wanted.lower()
            exact = [
                p for p in people
                if str(p.get("name", "")).strip().lower() == wanted_l and p.get("id")
            ]
            partial = [
                p for p in people
                if wanted_l in str(p.get("name", "")).strip().lower() and p.get("id")
            ]
            match = (exact or partial or [None])[0]
            if match:
                resolved.append((match.get("name") or wanted, match["id"]))
            else:
                log.warning("Immich person name %r was not found", wanted)

        _PERSON_IDS = resolved
        if _PERSON_IDS:
            log.info(
                "Immich people filter active: %s",
                ", ".join(f"{name}={person_id}" for name, person_id in _PERSON_IDS),
            )
        else:
            log.error("IMMICH_PERSON_NAMES is set but no matching Immich people were found")
        return _PERSON_IDS
    except Exception as exc:
        # Do not cache failures — Immich may still be starting when the
        # proxy service comes up; retry on the next /random-photo request.
        log.warning("failed to resolve Immich people %s: %s", PERSON_NAMES, exc)
        return []


def _sof_type(data: bytes) -> int | None:
    """Return the JPEG SOF marker byte (0xC0-0xCF) or None if not found."""
    scan = min(len(data) - 1, 4095)
    for i in range(scan):
        if data[i] == 0xFF and 0xC0 <= data[i + 1] <= 0xCF and data[i + 1] not in (0xC4, 0xC8, 0xCC):
            return data[i + 1]
    return None


def _to_baseline_jpeg(jpeg_raw: bytes, target_w: int = MAX_W, target_h: int = MAX_H, fill: bool = False) -> bytes:
    """Return a fixed-size, baseline JPEG that is friendly to JPEGDEC.

    The panel decoder is most reliable with 4:2:0 JPEGs whose dimensions are
    exact MCU multiples. Portrait/odd-sized Immich thumbnails are letterboxed
    into a fixed canvas instead of being served at their natural dimensions —
    unless fill=True, which instead scales the photo to cover the whole
    canvas and center-crops the overflow (no black bars, some edge loss).
    Default (fill=False) preserves the original letterbox behavior exactly.
    """
    img = Image.open(io.BytesIO(jpeg_raw))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    canvas_w = max(16, (target_w // 16) * 16)
    canvas_h = max(16, (target_h // 16) * 16)
    if fill:
        scale = max(canvas_w / img.width, canvas_h / img.height)
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.Resampling.LANCZOS)
        left = (img.width - canvas_w) // 2
        top = (img.height - canvas_h) // 2
        canvas = img.crop((left, top, left + canvas_w, top + canvas_h))
    else:
        img.thumbnail((canvas_w, canvas_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
        canvas.paste(img, ((canvas_w - img.width) // 2, (canvas_h - img.height) // 2))
    out = io.BytesIO()
    canvas.save(
        out,
        format="JPEG",
        quality=JPEG_QUALITY,
        optimize=False,
        progressive=False,
        subsampling=2,  # 4:2:0 on a 16px-aligned canvas is the safest JPEGDEC path.
    )
    return out.getvalue()


def _pick_landscape_preferred(assets: list) -> dict:
    """Return a landscape asset (width >= height) if one exists, else first asset."""
    if not LANDSCAPE_PREFER:
        return assets[0]
    landscape = [a for a in assets if (a.get("width") or 0) >= (a.get("height") or 0)]
    return (landscape or assets)[0]


def fetch_safe_jpeg(target_w: int = MAX_W, target_h: int = MAX_H, fill: bool = False) -> bytes | None:
    for attempt in range(RETRIES):
        try:
            batch = PORTRAIT_BATCH if LANDSCAPE_PREFER else 1
            search = {"size": batch, "type": "IMAGE"}
            person_filter = _resolve_person_ids()
            person_name = None
            if PERSON_NAMES:
                if not person_filter:
                    log.error("people filter configured but no people resolved")
                    return None
                person_name, person_id = random.choice(person_filter)
                search["personIds"] = [person_id]
            if FAVORITES_ONLY:
                search["isFavorite"] = True
            req_body = json.dumps(search).encode("utf-8")
            req = urllib.request.Request(
                f"{IMMICH_URL}/api/search/random",
                data=req_body,
                headers={
                    "x-api-key": IMMICH_KEY,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            arr = json.loads(raw)
            if not arr or not isinstance(arr, list):
                log.warning("attempt %d: bad JSON from random endpoint", attempt)
                continue
            images = [a for a in arr if a.get("type", "IMAGE") == "IMAGE" and a.get("id")]
            images = [a for a in images if not _is_ai_generated(a)]
            if not images:
                log.warning("attempt %d: no non-AI IMAGE assets in batch", attempt)
                continue
            item = _pick_landscape_preferred(images)
            is_landscape = (item.get("width") or 0) >= (item.get("height") or 0)
            asset_id = item["id"]

            jpeg_raw = _get(
                f"{IMMICH_URL}/api/assets/{asset_id}/thumbnail?size=preview",
                {"x-api-key": IMMICH_KEY, "Accept": "image/jpeg"},
            )

            sof = _sof_type(jpeg_raw)
            if sof is None:
                log.warning("attempt %d: no SOF marker found, retrying", attempt)
                continue
            # ALWAYS resize+re-encode. Full-size JPEGs (1440x1920 ≈ 5.5MB RGB565)
            # exhaust ESP32 PSRAM during decode and crash the device.
            result = _to_baseline_jpeg(jpeg_raw, target_w, target_h, fill)
            scope = f"person:{person_name}" if person_name else ("favorites" if FAVORITES_ONLY else "all")
            orient_tag = "landscape" if is_landscape else "portrait-fallback"
            log.info(
                "asset %s (%s, %s) SOF 0xFF%02X: %d → %d bytes (resized to <=%dx%d)",
                asset_id, scope, orient_tag, sof, len(jpeg_raw), len(result), target_w, target_h,
            )
            return result

        except Exception as exc:
            log.warning("attempt %d failed: %s", attempt, exc)

    log.error("all %d attempts failed", RETRIES)
    return None


def fetch_camera_snapshot(entity_name: str, size_w: int, size_h: int) -> bytes | None:
    """Fetch HA camera_proxy snapshot, resize, return JPEG bytes."""
    if not HA_URL or not HA_TOKEN:
        log.error("HA_URL / HA_TOKEN not configured for /camera endpoint")
        return None
    url = f"{HA_URL}/api/camera_proxy/camera.{entity_name}"
    try:
        raw = _get(url, {"Authorization": f"Bearer {HA_TOKEN}"}, timeout=5)
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((size_w, size_h), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(
            out,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=False,
            progressive=False,
            subsampling=0,  # 4:4:4 — safer for JPEGDEC on ESP32
        )
        result = out.getvalue()
        log.info("camera %s: %d → %d bytes (resized to <=%dx%d)",
                 entity_name, len(raw), len(result), size_w, size_h)
        return result
    except Exception as exc:
        log.warning("camera %s fetch failed: %s", entity_name, exc)
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # Immich slideshow
        if path == "/random-photo":
            query = parse_qs(parsed.query)
            try:
                target_w = min(ABS_MAX_W, max(16, int(query["w"][0])))
            except (KeyError, ValueError):
                target_w = MAX_W
            try:
                target_h = min(ABS_MAX_H, max(16, int(query["h"][0])))
            except (KeyError, ValueError):
                target_h = MAX_H
            fill = query.get("fill", ["0"])[0].lower() in ("1", "true", "yes", "on")
            jpeg = fetch_safe_jpeg(target_w, target_h, fill)
            if jpeg:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpeg)
            else:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Immich unavailable")
            return

        # /camera/<name>?size=thumb|full
        if path.startswith("/camera/"):
            tail = self.path[len("/camera/"):]
            if "?" in tail:
                name, q = tail.split("?", 1)
                size = "full" if "size=full" in q else "thumb"
            else:
                name, size = tail, "thumb"
            w, h = (800, 500) if size == "full" else (300, 200)
            jpeg = fetch_camera_snapshot(name, w, h)
            if jpeg:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpeg)
            else:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Camera unavailable")
            return

        self.send_response(404)
        self.end_headers()


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    log.info("Immich SOF-filter proxy starting on :%d  (Immich: %s)", PROXY_PORT, IMMICH_URL)
    ThreadedServer(("0.0.0.0", PROXY_PORT), Handler).serve_forever()
