#!/usr/bin/env bash
set -euo pipefail
# Fast GitHub-based deploy: git pull → cached docker compose build → health check.
#
# WHY THIS IS FAST: the VM keeps a git clone and the docker build-cache on disk.
# A one-line fix transfers only the git delta (a few KB, not a 100+MB tarball),
# and unchanged layers are reused (npm ci / pip install / apt are cached) — only
# the layers that depend on changed files re-build.
#
# Usage:  ./deploy/deploy.sh [vm-name]        (default: s3lm-budget)
# Env:    DEPLOY_BRANCH=main GCP_ACCOUNT GCP_PROJECT GCP_ZONE GCLOUD_BIN
source "$(dirname "${BASH_SOURCE[0]}")/gcp-config.sh"
PROJECT="$GCP_PROJECT"
VM=s3lm-budget
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -*) echo "Unknown option: $arg" >&2; exit 2 ;;
    *) VM="$arg" ;;
  esac
done
ZONE="$GCP_ZONE"
BRANCH="${DEPLOY_BRANCH:-main}"
APP=/opt/s3lm/s3-live-monitor

echo "→ deploy: $BRANCH → $VM ($GCP_ACCOUNT; $PROJECT/$ZONE)"
gcp_check_project

if ! STATE=$(gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" --format='get(status)'); then
  echo "✗ Cannot access VM $VM as $GCP_ACCOUNT; check the error above." >&2
  exit 1
fi
if [ "$CHECK_ONLY" = "1" ]; then
  echo "VM $VM: $STATE. Read-only deployment checks passed."
  exit 0
fi
if [ "$STATE" != "RUNNING" ]; then
  echo "  VM is $STATE — starting…"
  gcloud compute instances start "$VM" --project="$PROJECT" --zone="$ZONE" --quiet
  sleep 15
fi

echo -n "  waiting for SSH"
for _ in $(seq 1 30); do
  if gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --command="echo ok" --quiet 2>/dev/null | grep -q ok; then
    echo " — ready"; break
  fi
  echo -n "."; sleep 4
done

echo "→ pulling $BRANCH + rebuilding containers (cached)…"
gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command="
set -e
cd $APP
# tarball-era backups are stale once git owns this dir — remove them
rm -rf /opt/s3lm/s3-live-monitor.previous-* 2>/dev/null || true
if [ ! -d .git ]; then
  echo 'no git clone present — bootstrap missing'; exit 3
fi
git fetch origin
git reset --hard origin/$BRANCH
sudo docker compose up -d --build
sleep 6
echo '--- containers ---'
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
echo '--- health ---'
curl -fsS -m 10 http://127.0.0.1/api/healthz && echo || curl -fsS -m 10 http://127.0.0.1:80/api/healthz && echo
"

IP=$(gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
echo ""
echo "✅ Deployed $BRANCH → http://$IP/"
echo "   Health: http://$IP/api/healthz"
