#!/usr/bin/env bash
set -euo pipefail

mkdir -p /tmp/mig
cd /opt/s3lm/s3-live-monitor

git status --porcelain > /tmp/mig/vm-status.txt
git diff > /tmp/mig/vm-uncommitted.patch
git diff --stat > /tmp/mig/vm-diff-stat.txt
sudo cp .env /tmp/mig/app.env

for f in \
  /usr/local/bin/s3lm-watchdog.sh \
  /etc/cron.d/s3lm-watchdog \
  /usr/local/bin/backup_state_db.sh \
  /etc/cron.d/s3lm-db-backup \
  /etc/systemd/system/s3lm-tls.service; do
  if [ -e "$f" ]; then
    sudo cp "$f" "/tmp/mig/$(basename "$f")"
  fi
done

sudo tar -C /tmp/mig -czf /tmp/mig/small-critical.tgz .
sudo chown prism:prism /tmp/mig/small-critical.tgz
ls -lh /tmp/mig
sha256sum /tmp/mig/small-critical.tgz
