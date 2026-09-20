#!/bin/bash
set -euo pipefail
echo "=== containers ==="
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
echo "=== healthz ==="
curl -fsS -m 8 http://127.0.0.1/api/healthz; echo
echo "=== s3sync last logs ==="
sudo docker logs --since 20m s3sync 2>&1 | tail -n 25
echo "=== db audit ==="
sudo docker cp /tmp/audit_wallets.py s3lm:/tmp/audit_wallets.py
sudo docker exec s3lm python3 /tmp/audit_wallets.py
