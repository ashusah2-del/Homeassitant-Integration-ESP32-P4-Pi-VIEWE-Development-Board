#!/usr/bin/env bash
# ─── LAN endpoint mDNS → hub.env sync ─────────────────────────────────────────
# Neither the iHost (no static-IP setting) nor Home Assistant / the Pi can get a
# reserved DHCP lease on the Nokia router, so both IPs drift. Each advertises a
# stable mDNS name. This docker host resolves .local names (avahi); the hub
# container does not — so we resolve them here and keep the matching URLs in
# hub.env pointed at the current IPs, recreating the hub only when something
# actually changes.
#
#   iHost  ihost-1001ec4e82.local -> IHOST_URL=http://<ip>
#   HA     homeassistant.local    -> HA_URL=http://<ip>:8123
#
# Runs via sync-lan-endpoints.timer (systemd user service).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DIR/hub.env"
changed=0

log() { echo "sync-lan-endpoints: $*"; }

# sync_endpoint <mdns-name> <env-key> <url-prefix> <url-suffix>
# Resolves the name; if the IP differs from what's in ENV_FILE for env-key,
# rewrites the line. On resolve failure, leaves the last-known-good untouched.
sync_endpoint() {
  local host="$1" key="$2" prefix="$3" suffix="$4"
  local ip cur
  ip="$(getent hosts "$host" | awk '{print $1; exit}')"
  if [ -z "${ip:-}" ]; then
    log "cannot resolve $host — leaving $key unchanged"
    return
  fi
  cur="$(grep -oP "^${key}=https?://\K[0-9.]+" "$ENV_FILE" || true)"
  if [ "$ip" = "$cur" ]; then
    log "$key still at $ip — no change"
    return
  fi
  log "$key moved ${cur:-<unset>} -> $ip — updating hub.env"
  sed -i "s|^${key}=.*|${key}=${prefix}${ip}${suffix}|" "$ENV_FILE"
  changed=1
}

sync_endpoint "ihost-1001ec4e82.local" "IHOST_URL" "http://" ""
sync_endpoint "homeassistant.local"    "HA_URL"    "http://" ":8123"

if [ "$changed" = "1" ]; then
  cd "$DIR"
  docker compose up -d --force-recreate panel-hub
  log "hub recreated with updated endpoints"
fi
