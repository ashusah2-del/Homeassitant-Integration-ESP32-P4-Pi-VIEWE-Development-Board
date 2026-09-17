#!/usr/bin/env python3
"""Regenerate the Bluray-upgrade interactive button list on the HA
'Mangalam Pro' dashboard from sensor.radarr_upgrades_available.

Native HA button cards can't template their tap/hold `data`, so a dynamic
per-movie Upgrade/Discard list can't be done with stock cards. This script
rebuilds that block server-side and saves it via the authenticated Lovelace
websocket API. It is idempotent: it only writes when the config actually
changes, so it is safe to run on a short timer.

What it maintains (the "managed block"): the vertical-stack whose first card
is the markdown header '## ... Bluray Upgrade Candidates' + the tap/hold
subtitle. It replaces that stack's children with freshly generated rows and
removes the redundant static markdown *table* card (the '{% for m in movies %}'
one) so the candidate list appears exactly once, in the button design.

Env:
  HA_URL     e.g. http://192.168.55.4:8123   (falls back to HA_BASE)
  HA_TOKEN   long-lived access token
  DASH       dashboard url_path (default: mangalam-pro)
Flags:
  --dry-run  print what would change, do not save
"""
import asyncio, json, os, sys, urllib.request

HA_URL = (os.environ.get("HA_URL") or os.environ.get("HA_BASE") or "http://192.168.55.4:8123").rstrip("/")
TOKEN = os.environ["HA_TOKEN"].strip().strip('"')
DASH = os.environ.get("DASH", "mangalam-pro")
SENSOR = "sensor.radarr_upgrades_available"
DRY = "--dry-run" in sys.argv

SUBTITLE = ("_Upgrade: tap to queue · hold to search now | "
            "Discard: tap to skip · hold to restore_")
LABEL_STYLE = "ha-card { background: none; box-shadow: none; border: none; padding: 4px 8px 0; }"
HEADER_STYLE = "ha-card { background: none; box-shadow: none; border: none; }"


def movie_stack(m):
    mid = m["id"]
    def btn(name, icon, color, tap, hold):
        return {"type": "button", "name": name, "icon": icon, "icon_color": color,
                "tap_action": {"action": "perform-action", "perform_action": tap,
                               "data": {"movie_id": mid}},
                "hold_action": {"action": "perform-action", "perform_action": hold,
                                "data": {"movie_id": mid}}}
    label = f"**{m.get('title','?')}** ({m.get('year','')}) · {m.get('quality','')}"
    return {"type": "vertical-stack", "cards": [
        {"type": "markdown", "content": label, "style": LABEL_STYLE},
        {"type": "horizontal-stack", "cards": [
            btn("Upgrade to Bluray", "mdi:arrow-up-circle", "green",
                "rest_command.radarr_set_upgrade_profile", "rest_command.radarr_search_movie"),
            btn("Discard", "mdi:cancel", "red",
                "rest_command.radarr_add_no_upgrade", "rest_command.radarr_remove_no_upgrade"),
        ]},
    ]}


def build_managed(header_card, movies):
    header = {"type": "markdown",
              "content": f"## \U0001F3AC Bluray Upgrade Candidates ({len(movies)})\n\n{SUBTITLE}",
              "style": HEADER_STYLE}
    cards = [header]
    if movies:
        cards += [movie_stack(m) for m in movies]
    else:
        cards.append({"type": "markdown",
                      "content": "_All movies at best quality_ ✅",
                      "style": HEADER_STYLE})
    return {"type": "vertical-stack", "cards": cards}


def is_header_md(c):
    return (isinstance(c, dict) and c.get("type") == "markdown"
            and "Bluray Upgrade Candidates" in (c.get("content") or "")
            and "{% for" not in (c.get("content") or ""))


def is_table_card(c):
    return (isinstance(c, dict) and c.get("type") == "markdown"
            and "{% for m in movies" in (c.get("content") or ""))


def is_managed_stack(c):
    return (isinstance(c, dict) and c.get("type") == "vertical-stack"
            and (c.get("cards") or []) and is_header_md(c["cards"][0]))


def transform(container_cards, movies):
    """Mutate a list of cards in place: rebuild managed stack, drop the table
    card. Returns number of managed stacks rebuilt."""
    rebuilt = 0
    new = []
    for c in container_cards or []:
        if is_table_card(c):
            continue  # drop redundant static table
        if is_managed_stack(c):
            new.append(build_managed(c["cards"][0], movies))
            rebuilt += 1
            continue
        if isinstance(c, dict) and "cards" in c:
            rebuilt += transform(c["cards"], movies)
        new.append(c)
    container_cards[:] = new
    return rebuilt


def get_sensor():
    req = urllib.request.Request(f"{HA_URL}/api/states/{SENSOR}",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    movies = data.get("attributes", {}).get("movies", []) or []
    movies = sorted(movies, key=lambda m: (str(m.get("title", "")).lower(), m.get("year", 0)))
    return movies


async def main():
    import websockets
    movies = get_sensor()
    ws_url = HA_URL.replace("http", "ws", 1) + "/api/websocket"
    async with websockets.connect(ws_url, max_size=16_000_000) as ws:
        async def send(o): await ws.send(json.dumps(o))
        async def recv(): return json.loads(await ws.recv())
        await recv()  # auth_required
        await send({"type": "auth", "access_token": TOKEN})
        if (await recv()).get("type") != "auth_ok":
            print("AUTH FAILED", file=sys.stderr); sys.exit(2)
        await send({"id": 1, "type": "lovelace/config", "url_path": DASH})
        r = await recv()
        if not r.get("success"):
            print("get config failed:", r.get("error"), file=sys.stderr); sys.exit(2)
        cfg = r["result"]
        before = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        rebuilt = 0
        for v in cfg.get("views", []):
            rebuilt += transform(v.get("cards"), movies)
            for sec in (v.get("sections") or []):
                rebuilt += transform(sec.get("cards"), movies)
        after = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        if rebuilt == 0:
            print("WARN: no managed Bluray stack found (nothing rebuilt)", file=sys.stderr)
        if before == after:
            print(f"no change ({len(movies)} movies, {rebuilt} block(s))")
            return
        if DRY:
            print(f"DRY-RUN: would update {rebuilt} block(s), {len(movies)} movies")
            return
        await send({"id": 2, "type": "lovelace/config/save", "url_path": DASH, "config": cfg})
        resp = await recv()
        if not resp.get("success"):
            print("SAVE FAILED:", resp.get("error"), file=sys.stderr); sys.exit(2)
        print(f"saved: {rebuilt} block(s) rebuilt, {len(movies)} movies")


if __name__ == "__main__":
    asyncio.run(main())
