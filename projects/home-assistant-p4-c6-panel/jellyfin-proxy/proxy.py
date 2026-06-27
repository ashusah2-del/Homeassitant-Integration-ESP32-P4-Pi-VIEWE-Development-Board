#!/usr/bin/env python3
"""
Jellyfin LAN proxy for the ESP32-P4 panel.

Endpoints:
  GET  /health              -> 200 OK
  GET  /movies?start=0&limit=8 -> JSON movie list for the browse page
  GET  /poster/<item_id>    -> baseline JPEG poster (JPEGDEC-safe)
  POST /play/<item_id>      -> optional direct Jellyfin play on configured client
"""

import io
import json
import logging
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jellyfin-proxy")

JELLYFIN_URL = os.environ["JELLYFIN_URL"].rstrip("/")
JELLYFIN_KEY = os.environ["JELLYFIN_API_KEY"]
JELLYFIN_USER_ID = os.environ.get("JELLYFIN_USER_ID", "").strip()
JELLYFIN_PLAY_CLIENT = os.environ.get("JELLYFIN_PLAY_CLIENT", "").strip()
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8767"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "85"))
MAX_W = int(os.environ.get("MAX_W", "280"))
MAX_H = int(os.environ.get("MAX_H", "400"))

_USER_ID: str | None = None


def _headers() -> dict[str, str]:
    return {
        "X-Emby-Token": JELLYFIN_KEY,
        "Accept": "application/json",
    }


def _get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers or _headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url: str, timeout: int = 30):
    return json.loads(_get(url, timeout=timeout).decode("utf-8"))


def _post_json(url: str, payload: dict, timeout: int = 30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def _user_id() -> str:
    global _USER_ID
    if JELLYFIN_USER_ID:
        return JELLYFIN_USER_ID
    if _USER_ID:
        return _USER_ID
    users = _get_json(f"{JELLYFIN_URL}/Users")
    if not users:
        raise RuntimeError("no Jellyfin users found")
    _USER_ID = users[0]["Id"]
    log.info("Using Jellyfin user %s (%s)", users[0].get("Name"), _USER_ID)
    return _USER_ID


def _to_baseline_jpeg(jpeg_raw: bytes) -> bytes:
    img = Image.open(io.BytesIO(jpeg_raw))
    img = img.convert("RGB")
    canvas_w = max(16, (MAX_W // 16) * 16)
    canvas_h = max(16, (MAX_H // 16) * 16)
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
        subsampling=2,
    )
    return out.getvalue()


def _display_name(item: dict) -> str:
    orig = (item.get("OriginalTitle") or "").strip()
    if orig:
        return orig
    return (item.get("Name") or "Unknown").strip()


def fetch_movies(start: int, limit: int) -> dict:
    uid = _user_id()
    q = urllib.parse.urlencode(
        {
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "SortBy": "PremiereDate",
            "SortOrder": "Descending",
            "Fields": "ProductionYear,OriginalTitle,PremiereDate",
            "StartIndex": max(0, start),
            "Limit": max(1, min(limit, 20)),
        }
    )
    data = _get_json(f"{JELLYFIN_URL}/Users/{uid}/Items?{q}")
    items = data.get("Items", [])
    movies = []
    for item in items:
        movies.append(
            {
                "id": item.get("Id", ""),
                "name": _display_name(item),
                "year": item.get("ProductionYear") or 0,
            }
        )
    return {
        "total": int(data.get("TotalRecordCount", len(movies))),
        "start": max(0, start),
        "movies": movies,
    }


def fetch_poster(item_id: str) -> bytes | None:
    if not item_id:
        return None
    q = urllib.parse.urlencode(
        {"maxHeight": MAX_H, "maxWidth": MAX_W, "quality": JPEG_QUALITY, "format": "jpg"}
    )
    headers = {**_headers(), "Accept": "image/jpeg"}
    for image_type in ("Primary", "Poster"):
        url = f"{JELLYFIN_URL}/Items/{urllib.parse.quote(item_id)}/Images/{image_type}?{q}"
        try:
            jpeg_raw = _get(url, headers=headers)
            if not jpeg_raw:
                continue
            return _to_baseline_jpeg(jpeg_raw)
        except Exception as exc:
            log.debug("poster %s/%s failed: %s", item_id, image_type, exc)
    log.warning("poster fetch failed for %s (no Primary/Poster image)", item_id)
    return None


def play_on_client(item_id: str) -> tuple[int, str]:
    """Try to start playback on a Jellyfin client session by device name."""
    if not JELLYFIN_PLAY_CLIENT:
        return 501, "JELLYFIN_PLAY_CLIENT not configured"
    try:
        sessions = _get_json(f"{JELLYFIN_URL}/Sessions")
        target = None
        needle = JELLYFIN_PLAY_CLIENT.lower()
        for session in sessions:
            device = str(session.get("DeviceName", "")).lower()
            client = str(session.get("Client", "")).lower()
            if needle in device or needle in client:
                target = session
                break
        if not target:
            return 404, f"No active Jellyfin session matching {JELLYFIN_PLAY_CLIENT!r}"

        sid = target["Id"]
        _post_json(
            f"{JELLYFIN_URL}/Sessions/{sid}/Playing",
            {
                "ItemIds": [item_id],
                "PlayCommand": "PlayNow",
                "StartPositionTicks": 0,
            },
        )
        log.info("Started %s on session %s (%s)", item_id, sid, target.get("DeviceName"))
        return 200, "ok"
    except Exception as exc:
        log.error("play failed: %s", exc)
        return 500, str(exc)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = {}
        if "?" in self.path:
            query = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1]))

        if path == "/health":
            self._send_json(200, {"ok": True})
            return

        if path == "/movies":
            start = int(query.get("start", "0") or 0)
            limit = int(query.get("limit", "8") or 8)
            try:
                payload = fetch_movies(start, limit)
                self._send_json(200, payload)
            except Exception as exc:
                log.error("movies fetch failed: %s", exc)
                self._send_json(503, {"error": str(exc), "movies": [], "total": 0, "start": start})
            return

        if path.startswith("/poster/"):
            item_id = urllib.parse.unquote(path[len("/poster/"):])
            jpeg = fetch_poster(item_id)
            if jpeg:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpeg)
            else:
                self.send_response(404)
                self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/play/"):
            item_id = urllib.parse.unquote(path[len("/play/"):])
            code, msg = play_on_client(item_id)
            self._send_json(code, {"ok": code == 200, "message": msg})
            return
        self.send_response(404)
        self.end_headers()


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    log.info("Jellyfin proxy starting on :%d (server: %s)", PROXY_PORT, JELLYFIN_URL)
    ThreadedServer(("0.0.0.0", PROXY_PORT), Handler).serve_forever()
