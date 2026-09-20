#!/bin/bash
set -euo pipefail
TOKEN=$(sudo docker exec s3lm cat /root/.local/share/s3-live-monitor/access-token.txt)
CUTOFF=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
echo "CUTOFF=$CUTOFF"
URL="http://127.0.0.1/api/mule-rails?priority=high&limit=8&source=object&active_since=${CUTOFF}"
curl -s -m 30 -H "Authorization: Bearer ${TOKEN}" "$URL" -o /tmp/_mule24.json
python3 - <<'PY'
import json
d=json.load(open("/tmp/_mule24.json"))
print("total", d.get("total"))
print("by_rail_type", (d.get("summary") or {}).get("by_rail_type"))
items=d.get("items") or []
print("n_items", len(items))
for i in items:
    holders=(i.get("account_holders") or "")[:90]
    print(i.get("priority"), i.get("candidate_score"), i.get("rail_type"), i.get("rail_id"),
          "gamblers", i.get("gamblers"), "deposits", i.get("deposit_transactions"),
          "last", i.get("last_seen"), holders)
PY
curl -s -m 5 http://127.0.0.1/api/healthz; echo
cd /opt/s3lm/s3-live-monitor && git log -1 --oneline
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
