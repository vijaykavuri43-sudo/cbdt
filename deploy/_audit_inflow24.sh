#!/bin/bash
set -euo pipefail
TOKEN=$(sudo docker exec s3lm cat /root/.local/share/s3-live-monitor/access-token.txt)
CUTOFF=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
echo "CUTOFF=$CUTOFF"
curl -s -m 30 -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1/api/mule-rails?limit=8&source=object&active_since=${CUTOFF}" \
  -o /tmp/_inflow24.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/_inflow24.json"))
print("total", d.get("total"))
print("by_rail_type", (d.get("summary") or {}).get("by_rail_type"))
print("priority_counts", (d.get("summary") or {}).get("priority_counts"))
for i in (d.get("items") or []):
    print(
        i.get("rail_type"),
        i.get("rail_id"),
        "win_amt", i.get("window_deposit_amount_inr"),
        "win_tx", i.get("window_deposit_transactions"),
        "life_amt", i.get("deposit_amount_inr"),
        "last", i.get("window_last_seen") or i.get("last_seen"),
    )
PY
curl -s -m 5 http://127.0.0.1/api/healthz; echo
cd /opt/s3lm/s3-live-monitor && git log -1 --oneline
