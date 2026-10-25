#!/usr/bin/env bash
set -euo pipefail

sudo mkdir -p /tmp/emergency

for spec in \
  "s3lm:/root/.local/share/s3-live-monitor/manifest.db:manifest-b1.db" \
  "s3lm-b2:/root/.local/share/s3-live-monitor/manifest.db:manifest-b2.db" \
  "s3lm-b3:/root/.local/share/s3-live-monitor/manifest.db:manifest-b3.db" \
  "s3sync:/root/.local/share/s3-sync/state.db:state-b1.db" \
  "s3sync-b2:/root/.local/share/s3-sync/state.db:state-b2.db" \
  "s3sync-b3:/root/.local/share/s3-sync/state.db:state-b3.db"; do
  c=${spec%%:*}
  rest=${spec#*:}
  db=${rest%%:*}
  out=${rest##*:}
  echo "== snapshot $c:$db -> $out"
  sudo docker exec "$c" python3 -c \
    "import sqlite3;sqlite3.connect('$db').execute(\"VACUUM INTO '/tmp/$out'\")"
  sudo docker cp "$c:/tmp/$out" "/tmp/emergency/$out"
  sudo docker exec "$c" rm -f "/tmp/$out"
done

echo "== archiving app checkout + intelligence"
set +e
sudo tar --warning=no-file-changed -C /opt/s3lm -czf /tmp/emergency/app-and-intelligence.tgz \
  s3-live-monitor intelligence
tar_rc=$?
set -e
if [ "$tar_rc" -gt 1 ]; then
  echo "tar failed with exit code $tar_rc" >&2
  exit "$tar_rc"
fi

echo "== archiving decoded DBs"
sudo tar -C /tmp/emergency -czf "/tmp/emergency/decoded-dbs-$(date -u +%F).tgz" \
  manifest-b1.db manifest-b2.db manifest-b3.db state-b1.db state-b2.db state-b3.db
sudo rm -f /tmp/emergency/manifest-*.db /tmp/emergency/state-*.db
sudo chown "$USER":"$USER" /tmp/emergency/*.tgz
sha256sum /tmp/emergency/*.tgz | tee /tmp/emergency/SHA256SUMS
du -sh /tmp/emergency/*
