#!/bin/bash
set -euo pipefail
TOKEN=$(sudo docker exec s3lm cat /root/.local/share/s3-live-monitor/access-token.txt)
CUTOFF=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
echo "CUTOFF=$CUTOFF"
for TYPE in crypto_wallet bank_account; do
  echo "=== $TYPE ==="
  curl -s -m 30 -H "Authorization: Bearer ${TOKEN}" \
    "http://127.0.0.1/api/mule-rails?priority=high&limit=5&source=object&rail_type=${TYPE}&active_since=${CUTOFF}" \
    -o /tmp/_mule_type.json
  python3 - <<'PY'
import json
d=json.load(open("/tmp/_mule_type.json"))
print("total", d.get("total"))
for i in (d.get("items") or []):
    print(i.get("priority"), i.get("candidate_score"), i.get("rail_type"), i.get("rail_id"),
          "gamblers", i.get("gamblers"), "deposits", i.get("deposit_transactions"),
          "amt", i.get("deposit_amount_inr"), "last", i.get("last_seen"))
PY
done
