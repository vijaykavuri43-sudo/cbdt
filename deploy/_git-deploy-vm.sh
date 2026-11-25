#!/bin/bash
set -euo pipefail
APP=/opt/s3lm/s3-live-monitor
cd "$APP"
echo "--- before ---"
git log -1 --oneline || true
# VM has no GitHub SSH key; fetch over HTTPS.
git fetch https://github.com/vijaykavuri43-sudo/cbdt.git main
git reset --hard FETCH_HEAD
echo "--- after ---"
git log -1 --oneline
git rev-parse HEAD
sudo docker compose up -d --build
sleep 8
echo "--- containers ---"
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
echo "--- health ---"
curl -fsS -m 15 http://127.0.0.1/api/healthz
echo
