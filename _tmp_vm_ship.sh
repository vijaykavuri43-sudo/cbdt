#!/bin/bash
set -euo pipefail
APP=/opt/s3lm/s3-live-monitor
cd "$APP"

# Incoming files were copied to /tmp/s3lm-ship/
SRC=/tmp/s3lm-ship
cp -f "$SRC/backend/db.py" backend/db.py
cp -f "$SRC/backend/app.py" backend/app.py
cp -f "$SRC/backend/test_officer_query_bounds.py" backend/test_officer_query_bounds.py
cp -f "$SRC/frontend/src/App.tsx" frontend/src/App.tsx
cp -f "$SRC/frontend/src/pages/Explorer.tsx" frontend/src/pages/Explorer.tsx
cp -f "$SRC/frontend/src/pages/Status.tsx" frontend/src/pages/Status.tsx
cp -f "$SRC/frontend/src/pages/OfficerQuery.tsx" frontend/src/pages/OfficerQuery.tsx
cp -f "$SRC/frontend/src/lib/api.ts" frontend/src/lib/api.ts
cp -f "$SRC/frontend/src/index.css" frontend/src/index.css
cp -f "$SRC/frontend/src/components/CommandPalette.tsx" frontend/src/components/CommandPalette.tsx
cp -f "$SRC/frontend/src/components/OfficerStrip.tsx" frontend/src/components/OfficerStrip.tsx
rm -f frontend/src/pages/Search.tsx

git add -A backend/db.py backend/app.py backend/test_officer_query_bounds.py \
  frontend/src/App.tsx frontend/src/pages/Explorer.tsx frontend/src/pages/Status.tsx \
  frontend/src/pages/OfficerQuery.tsx frontend/src/lib/api.ts frontend/src/index.css \
  frontend/src/components/CommandPalette.tsx frontend/src/components/OfficerStrip.tsx
git add -u frontend/src/pages/Search.tsx || true

git status --short
if ! git diff --cached --quiet; then
  git commit -m "Officer dashboard: Date range tab, Explorer name/path search, tighter Overview, drop Search tab"
  git push origin HEAD || echo "WARN: git push failed — continuing with local tree rebuild"
else
  echo "nothing to commit — rebuilding anyway"
fi

sudo docker compose up -d --build
sleep 8
echo '--- containers ---'
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
echo '--- health ---'
curl -fsS -m 10 http://127.0.0.1/api/healthz && echo
