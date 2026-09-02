#!/usr/bin/env bash
# S3 Live Monitor — start backend (:8787) + frontend (:5173)
# CASE-2026-CBTF · STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING · READ-ONLY bucket access
set -e
cd "$(dirname "$0")"

# Unify DB location: use s3-live-monitor/data/manifest.db when present (423M live mirror),
# otherwise fall back to ~/.local/share default. Prevents split-brain where home DB is empty (0 objects).
if [ -z "${S3LM_DATA:-}" ]; then
  if [ -f "data/manifest.db" ]; then
    export S3LM_DATA="$(pwd)/data"
  elif [ -f "/mnt/c/Users/prism/E-DRIVE/CRICKET-BETTING/CBDT-CRICK-BETTING/s3-live-monitor/data/manifest.db" ]; then
    export S3LM_DATA="/mnt/c/Users/prism/E-DRIVE/CRICKET-BETTING/CBDT-CRICK-BETTING/s3-live-monitor/data"
  fi
fi

echo "[*] starting backend on 127.0.0.1:8787 ... (S3LM_DATA=${S3LM_DATA:-default})"
( setsid python3 -m uvicorn backend.app:app --host "${S3LM_HOST:-127.0.0.1}" --port "${S3LM_PORT:-8787}" \
    > /tmp/s3lm-backend.log 2>&1 < /dev/null & )

sleep 2
echo "[*] starting frontend on :5173 (proxy -> backend) ..."
( setsid npm --prefix frontend run dev > /tmp/s3lm-frontend.log 2>&1 < /dev/null & )

sleep 4
curl -s -m 10 http://127.0.0.1:8787/api/healthz >/dev/null && echo "[+] backend OK" || echo "[!] backend not up yet — check /tmp/s3lm-backend.log"
echo "[+] dashboard: http://localhost:5173"
echo "    logs: /tmp/s3lm-backend.log /tmp/s3lm-frontend.log"
