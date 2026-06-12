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
import struct
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("immich-proxy")

IMMICH_URL   = os.environ["IMMICH_URL"].rstrip("/")
IMMICH_KEY   = os.environ["IMMICH_API_KEY"]
PROXY_PORT   = int(os.environ.get("PROXY_PORT", "8765"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "85"))
RETRIES      = int(os.environ.get("RETRIES", "8"))
# Home Assistant — only needed for the /camera/<name> endpoint.
HA_URL       = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN     = os.environ.get("HA_TOKEN", "")
# Cap output dimensions — the ESP32-P4 panel decodes to RGB565 in PSRAM and
# decoding a 1440x1920 JPEG needs ~5.5MB working memory which causes crashes.
# 800x600 is plenty for the 684x600 display area and decodes in ~960KB.
MAX_W        = int(os.environ.get("MAX_W", "800"))
MAX_H        = int(os.environ.get("MAX_H", "600"))


def _get(url: str, headers: dict, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _sof_type(data: bytes) -> int | None:
    """Return the JPEG SOF marker byte (0xC0-0xCF) or None if not found."""
    scan = min(len(data) - 1, 4095)
    for i in range(scan):
        if data[i] == 0xFF and 0xC0 <= data[i + 1] <= 0xCF and data[i + 1] not in (0xC4, 0xC8, 0xCC):
            return data[i + 1]
    return None


def _to_baseline_jpeg(jpeg_raw: bytes) -> bytes:
    """Re-encode via Pillow — always SOF0 baseline, resized to fit MAX_W x MAX_H.

    subsampling=2 forces 4:2:0 chroma which halves the Cb/Cr data and
    cuts JPEGDEC's per-MCU work on the ESP32-P4 by ~30 %. Combined with
    a smaller MAX_W it brings the worst-case slideshow decode from
    ~5 s back under the 5 s task-watchdog window.
    """
    img = Image.open(io.BytesIO(jpeg_raw))
    img = img.convert("RGB")
    img.thumbnail((MAX_W, MAX_H), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(
        out,
        format="JPEG",
        quality=JPEG_QUALITY,
        optimize=False,
        progressive=False,
        subsampling=2,
    )
    return out.getvalue()


def fetch_safe_jpeg() -> bytes | None:
    for attempt in range(RETRIES):
        try:
            # Pull from favourites only — POST /api/search/random with
            # isFavorite:true. The legacy /api/assets/random endpoint has no
            # favourite filter, so we use the search/random endpoint instead.
            req_body = json.dumps({"size": 1, "type": "IMAGE", "isFavorite": True}).encode("utf-8")
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
            item = arr[0]
            if item.get("type", "IMAGE") != "IMAGE":
                log.warning("attempt %d: type=%s, retrying", attempt, item.get("type"))
                continue
            asset_id = item.get("id")
            if not asset_id:
                continue

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
            result = _to_baseline_jpeg(jpeg_raw)
            log.info(
                "asset %s SOF 0xFF%02X: %d → %d bytes (resized to <=%dx%d)",
                asset_id, sof, len(jpeg_raw), len(result), MAX_W, MAX_H,
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
        raw = _get(url, {"Authorization": f"Bearer {HA_TOKEN}"})
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail((size_w, size_h), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(
            out,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=False,
            progressive=False,
            subsampling=2,
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
        # Immich slideshow
        if self.path == "/random-photo":
            jpeg = fetch_safe_jpeg()
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
        if self.path.startswith("/camera/"):
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
