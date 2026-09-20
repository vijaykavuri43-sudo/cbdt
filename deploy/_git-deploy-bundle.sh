#!/bin/bash
set -euo pipefail
APP=/opt/s3lm/s3-live-monitor
cd "$APP"
echo "--- before ---"
git log -1 --oneline || true
if [ -f /tmp/_s3lm-head.bundle ]; then mv /tmp/_s3lm-head.bundle /tmp/s3lm-head.bundle; fi
git fetch /tmp/s3lm-head.bundle HEAD:refs/heads/bundle-head
git reset --hard bundle-head
echo "--- after ---"
git log -1 --oneline
sudo docker compose up -d --build
sleep 12
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
curl -fsS -m 15 http://127.0.0.1/api/healthz; echo
