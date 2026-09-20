#!/bin/bash
set -euo pipefail
if [ -d /tmp/s3lm-frontend-src/src ]; then
  SRC=/tmp/s3lm-frontend-src/src
else
  SRC=/tmp/s3lm-frontend-src
fi
echo "SRC=$SRC"
sudo cp -a "$SRC/." /opt/s3lm/s3-live-monitor/frontend/src/
sudo cp /tmp/s3lm-backend/*.py /opt/s3lm/s3-live-monitor/backend/
echo "--- App nav ---"
grep -n "AI Investigator\|evidence\|Mule rails" /opt/s3lm/s3-live-monitor/frontend/src/App.tsx | head
echo "--- wallet export ---"
grep -n "all_source_files" /opt/s3lm/s3-live-monitor/backend/app.py | head
cd /opt/s3lm/s3-live-monitor
sudo docker compose up -d --build
curl -s -m 15 http://127.0.0.1/api/healthz || true
echo
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
