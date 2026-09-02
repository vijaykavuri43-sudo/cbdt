#!/usr/bin/env bash
# Stop S3 Live Monitor backend + frontend
for pat in "uvicorn backend.app:app" "vite"; do
  for pid in $(pgrep -f "$pat"); do
    kill "$pid" 2>/dev/null && echo "stopped $pid ($pat)"
  done
done
