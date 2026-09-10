#!/usr/bin/env bash
# Nightly online backup of the live SQLite DBs to GCS (run ON THE VM as root).
#
# WHY: decode-once lives here — manifest.db holds every decoded payload
# (UPI/VPA/wallet/payout rows the dashboard + CSV exports read). Losing the
# volume would force a full 1.5M-object re-decode (~200h+ backlog).
# Uses SQLite VACUUM INTO (WAL-safe consistent snapshot, no downtime).
#
# Install on VM (once):
#   sudo cp scripts/backup_state_db.sh /usr/local/bin/
#   echo '0 3 * * * root /usr/local/bin/backup_state_db.sh' | sudo tee /etc/cron.d/s3lm-db-backup
#
# Restore: gcloud storage cp gs://<bucket>/_backups/<date>/manifest.db <dest>
# then stop s3lm, replace the volume file, start s3lm.
set -euo pipefail

BUCKET="${S3LM_GCS_BUCKET:-cbtf-case-evidence-518405669914}"
TS="$(date -u +%F)"
DEST="gs://${BUCKET}/_backups/${TS}/"

backup_one() { # container, db-path-in-container, out-name
  local c="$1" db="$2" out="$3"
  sudo docker exec "$c" python3 -c \
    "import sqlite3;sqlite3.connect('${db}').execute(\"VACUUM INTO '/tmp/${out}'\")"
  sudo docker cp "${c}:/tmp/${out}" "/tmp/${out}"
  sudo docker exec "$c" rm -f "/tmp/${out}"
  gcloud storage cp "/tmp/${out}" "${DEST}${out}"
  rm -f "/tmp/${out}"
  echo "backed up ${out} -> ${DEST}${out}"
}

backup_one s3lm   /root/.local/share/s3-live-monitor/manifest.db manifest.db
backup_one s3sync /root/.local/share/s3-sync/state.db            s3sync-state.db
