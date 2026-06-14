#!/bin/bash
# Run this once after linking your Smart Life account to iot.tuya.com project.
# It fetches all device local keys and updates devices.json automatically.
#
# Steps before running:
#   1. Go to https://iot.tuya.com → your project → Devices → Link App Account
#   2. Scan the QR code with the Tuya / Smart Life app
#   3. Run this script

set -e
cd "$(dirname "$0")"

API_KEY="nyfujgum7xdvvfn5sdtn"
API_SECRET="d085f1089dac478796af354cf2058ea4"
API_REGION="eu"

echo "Fetching device list from Tuya IoT..."
python3 - <<EOF
import tinytuya, json, sys

c = tinytuya.Cloud(
    apiRegion='$API_REGION',
    apiKey='$API_KEY',
    apiSecret='$API_SECRET',
)
devices = c.getdevices(verbose=True)
if not devices.get('success', True) and devices.get('result') is None:
    print('Error:', devices, file=sys.stderr)
    sys.exit(1)

result = devices if isinstance(devices, list) else devices.get('result', [])
print(f'Found {len(result)} devices')

# Build key updates list
updates = []
for d in result:
    did = d.get('id')
    key = d.get('local_key')
    name = d.get('name', 'unknown')
    if did and key:
        updates.append({'id': did, 'key': key})
        print(f'  {name}: {did} → key={key[:4]}****')
    else:
        print(f'  SKIPPED {name}: {did} (no key)')

if not updates:
    print('No keys found. Make sure you linked the Smart Life app account first.', file=sys.stderr)
    sys.exit(1)

# POST to running bridge
import urllib.request
req = urllib.request.Request(
    'http://localhost:8766/keys',
    data=json.dumps(updates).encode(),
    headers={'Content-Type': 'application/json'},
    method='POST',
)
with urllib.request.urlopen(req, timeout=10) as r:
    resp = json.loads(r.read())
print('Bridge updated:', resp)
EOF
