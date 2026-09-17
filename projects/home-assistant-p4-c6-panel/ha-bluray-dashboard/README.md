# ha-bluray-dashboard

Auto-generates the **Bluray Upgrade Candidates** interactive button list on the
Home Assistant *Mangalam Pro* dashboard (`url_path: mangalam-pro`) from
`sensor.radarr_upgrades_available`.

## Why a generator

Native HA button cards can't template their tap/hold `data`, so a per-movie
Upgrade/Discard button list can't be built with stock cards from a sensor.
`generate.py` builds the block server-side and writes it via the authenticated
Lovelace websocket API (`lovelace/config/save`). It is **idempotent** — it only
saves when the rendered config actually changes, so it is safe on a short timer.

## What it maintains

The "managed block" is the `vertical-stack` whose first card is the markdown
header `## 🎬 Bluray Upgrade Candidates …`. Each run replaces that stack's
children with one row per movie:

```
**Title** (Year) · Quality
[ Upgrade to Bluray ]  [ Discard ]
```

- Upgrade: tap → `rest_command.radarr_set_upgrade_profile`, hold → `radarr_search_movie`
- Discard: tap → `rest_command.radarr_add_no_upgrade`, hold → `radarr_remove_no_upgrade`

It also removes the old redundant static markdown *table* card
(`{% for m in movies %}`) so the list appears once, in the button design.

## Deploy (Docker host, systemd --user)

```bash
mkdir -p ~/docker/ha-bluray-dashboard
cp generate.py ~/docker/ha-bluray-dashboard/
cp bluray-dashboard.env.example ~/docker/ha-bluray-dashboard/bluray-dashboard.env
# edit bluray-dashboard.env → set a long-lived HA_TOKEN (chmod 600)
cp bluray-dashboard.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bluray-dashboard.timer
loginctl enable-linger "$USER"   # run without an active login
```

Runs 2 min after boot, then every 5 min. Manual run / dry-run:

```bash
set -a; . ~/docker/ha-bluray-dashboard/bluray-dashboard.env; set +a
python3 ~/docker/ha-bluray-dashboard/generate.py --dry-run
```

Requires Python 3 with the `websockets` package on the host.

## Notes

- `bluray-dashboard.env` holds the HA token — keep it out of git (`.gitignore`).
- A backup of the dashboard before first run is at
  `.storage/lovelace.mangalam_pro.bak.*` on the HA host.
