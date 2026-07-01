#!/usr/bin/env bash
# Press the physical button on the iHost first, then run this within 5 minutes.
# The token is written to proxy.env automatically.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/proxy.env"
IHOST_URL="${IHOST_URL:-http://ihost.local}"

echo "Requesting token from ${IHOST_URL} ..."
RESP=$(curl -sf "${IHOST_URL}/open-api/v2/rest/bridge/access_token?app_name=panel")
echo "Response: ${RESP}"

TOKEN=$(echo "${RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['token'])" 2>/dev/null || true)
if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: Could not extract token."
  echo "       Did you press the iHost button within the last 5 minutes?"
  echo "       Full response: ${RESP}"
  exit 1
fi

echo "Token: ${TOKEN}"

if [[ -f "${ENV_FILE}" ]]; then
  if grep -q "^IHOST_TOKEN=" "${ENV_FILE}"; then
    sed -i "s|^IHOST_TOKEN=.*|IHOST_TOKEN=${TOKEN}|" "${ENV_FILE}"
  else
    echo "IHOST_TOKEN=${TOKEN}" >> "${ENV_FILE}"
  fi
else
  cp "${SCRIPT_DIR}/proxy.env.example" "${ENV_FILE}"
  sed -i "s|^IHOST_TOKEN=.*|IHOST_TOKEN=${TOKEN}|" "${ENV_FILE}"
fi

echo "Token written to ${ENV_FILE}"
echo ""
echo "Next steps:"
echo "  systemctl --user restart ihost-proxy   # restart if already running"
echo "  curl http://localhost:8768/devices      # list devices to get serial numbers"
