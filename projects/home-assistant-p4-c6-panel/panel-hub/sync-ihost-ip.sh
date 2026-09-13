#!/usr/bin/env bash
# ─── iHost mDNS → hub.env IP sync ─────────────────────────────────────────────
# The iHost has no static-IP setting and the Nokia router can't reserve DHCP
# leases, so its IP drifts. But it advertises a stable mDNS name. This docker
# host resolves .local names (avahi); the hub container does not. So we resolve
# the name here and keep IHOST_URL in hub.env pointed at the current IP,
# recreating the hub only when the IP actually changes.
#
# Runs via sync-ihost-ip.timer (systemd user service).
set -euo pipefail

IHOST_HOST="ihost-1001ec4e82.local"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DIR/hub.env"

log() { echo "sync-ihost-ip: $*"; }

# Resolve the mDNS name. On failure, leave the last-known-good IP untouched
# rather than flapping the hub offline.
ip="$(getent hosts "$IHOST_HOST" | awk '{print $1; exit}')"
if [ -z "${ip:-}" ]; then
  log "cannot resolve $IHOST_HOST — leaving hub.env unchanged"
  exit 0
fi

current="$(grep -oP '^IHOST_URL=https?://\K[0-9.]+' "$ENV_FILE" || true)"
if [ "$ip" = "$current" ]; then
  log "iHost still at $ip — no change"
  exit 0
fi

log "iHost moved ${current:-<unset>} -> $ip — updating hub.env and recreating hub"
sed -i "s|^IHOST_URL=.*|IHOST_URL=http://$ip|" "$ENV_FILE"

cd "$DIR"
docker compose up -d --force-recreate panel-hub
log "hub recreated with IHOST_URL=http://$ip"
