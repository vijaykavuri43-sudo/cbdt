#!/usr/bin/env bash
# Install the tolerant s3lm watchdog on the VM (run ON THE VM as root, once).
# Mirrors the pattern of scripts/backup_state_db.sh (repo-tracked script +
# cron file install).
#
#   sudo bash /opt/s3lm/s3-live-monitor/scripts/install-watchdog.sh
#
# Installs:
#   /usr/local/bin/s3lm-watchdog.sh   (copy of scripts/s3lm-watchdog.sh)
#   /etc/cron.d/s3lm-watchdog          (1-min cadence, flock-guarded)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/s3lm-watchdog.sh"
if [ ! -f "$SRC" ]; then
  echo "missing $SRC" >&2
  exit 1
fi
if [ "$(id -u)" != "0" ]; then
  echo "run as root (sudo)" >&2
  exit 1
fi

cp "$SRC" /usr/local/bin/s3lm-watchdog.sh
chmod 755 /usr/local/bin/s3lm-watchdog.sh
mkdir -p /var/tmp/s3lm-watchdog

cat > /etc/cron.d/s3lm-watchdog <<'EOF'
# Tolerant s3lm watchdog: restart only after 3 consecutive failed health
# checks, never within 5 min of a (re)start. Replaces the hair-trigger
# `curl || docker restart s3lm` that SIGKILL-looped the dashboard
# (review 2026-09-17). Installed by scripts/install-watchdog.sh.
* * * * * root flock -n /var/tmp/s3lm-watchdog.lock /usr/local/bin/s3lm-watchdog.sh
EOF
chmod 644 /etc/cron.d/s3lm-watchdog

echo "installed: /usr/local/bin/s3lm-watchdog.sh + /etc/cron.d/s3lm-watchdog"
cat /etc/cron.d/s3lm-watchdog | tail -n 2
