#!/usr/bin/env bash
set -euo pipefail
# Deploy S3 Live Monitor to a 4-vCPU GCP VM sized for concurrent OCR + sync.
# Usage:  ./deploy/gcp-deploy.sh [vm-name]   — defaults to s3lm-budget

PROJECT="${GCP_PROJECT:-learn2earn-4f6a5}"
VM="${1:-s3lm-budget}"
ZONE="${GCP_ZONE:-asia-south1-a}"
REGION="${GCP_REGION:-asia-south1}"
MACHINE="${GCP_MACHINE:-e2-standard-4}"
DISK_GB="${DISK_GB:-40}"

echo "→ Project: $PROJECT  VM: $VM  Zone: $ZONE  Machine: $MACHINE  Disk: ${DISK_GB}GB"

# 1. enable APIs (idempotent)
gcloud services enable compute.googleapis.com --project="$PROJECT" >/dev/null 2>&1 || true

# 2. firewall for 80/443 if missing
for rule in s3lm-allow-http s3lm-allow-https; do
  port=$([ "$rule" = s3lm-allow-http ] && echo 80 || echo 443)
  if ! gcloud compute firewall-rules describe "$rule" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud compute firewall-rules create "$rule" \
      --project="$PROJECT" --network=default --allow="tcp:$port" \
      --source-ranges=0.0.0.0/0 --target-tags="$rule" >/dev/null
    echo "  created firewall $rule (tcp:$port)"
  fi
done

# 3. create VM if it doesn't exist; start it if it's stopped
if ! gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$VM" \
    --project="$PROJECT" --zone="$ZONE" \
    --machine-type="$MACHINE" \
    --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
    --boot-disk-size="${DISK_GB}GB" --boot-disk-type=pd-balanced \
    --tags="s3lm-allow-http,s3lm-allow-https" \
    --metadata=startup-script='#!/bin/bash
set -e
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq docker.io docker-compose-plugin
  systemctl enable --now docker
fi
# 2GB emergency swap; normal OCR/sync work should remain in RAM.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null || true
fi
' \
  echo "  VM $VM created — waiting for startup script…"
  sleep 45
else
  STATE=$(gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" --format="get(status)")
  if [ "$STATE" != "RUNNING" ]; then
    echo "  VM $VM is $STATE — starting…"
    gcloud compute instances start "$VM" --project="$PROJECT" --zone="$ZONE" --quiet
    sleep 20
  else
    echo "  VM $VM already running"
  fi
fi

# 4. wait for SSH
echo -n "  waiting for SSH"
for i in $(seq 1 30); do
  if gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --command="echo ok" --quiet 2>/dev/null | grep -q ok; then echo " — ready"; break; fi
  echo -n "."; sleep 4
done

IP=$(gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" --format="get(networkInterfaces[0].accessConfigs[0].natIP)")
echo "  VM IP: $IP"

# 5. copy app + case reference data (rsync via gcloud scp)
echo "→ copying s3-live-monitor/ + reference data to VM…"
REPO_DIR="$(dirname "$0")/../.."
TARBALL="${TMPDIR:-/tmp}/s3lm.tgz"
# NOTE: --exclude MUST precede the paths (tar positional-arg quirk) —
# after the paths it silently does nothing (bit us on 2026-09-02: 128MB
# tarball shipping frontend/node_modules).
tar -czf "$TARBALL" \
  --exclude="s3-live-monitor/frontend/node_modules" \
  --exclude="s3-live-monitor/frontend/dist" \
  --exclude="s3-live-monitor/data" \
  --exclude="s3-live-monitor/backend/__pycache__" \
  --exclude="s3-live-monitor/.env" \
  -C "$REPO_DIR" \
  s3-live-monitor \
  LE-SUBMISSION-MASTER/17-CRYPTO-WALLETS.csv \
  intelligence/analysis/CASE-2026-CBTF/mule-accounts/extracted/unique-upi-list-full.txt \
  intelligence/analysis/CASE-2026-CBTF/preservation \
  intelligence/exports/CASE-2026-CBTF-5DAY-2026-08-14-to-2026-08-19/data/analysis.utr-gambler.tsv \
  intelligence/exports/CASE-2026-CBTF-5DAY-2026-08-14-to-2026-08-19/data/analysis.utr-vpa-join.tsv \
  intelligence/audit \
  intelligence/registry 2>/dev/null
echo "  tarball: $(du -h "$TARBALL" | cut -f1)"
gcloud compute scp "$TARBALL" "$VM":/tmp/s3lm.tgz --project="$PROJECT" --zone="$ZONE" --quiet
gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command="
  set -e
  # /opt/s3lm is the repo-root equivalent on the VM (matches docker-compose ../ mounts)
  sudo rm -rf /opt/s3lm && sudo mkdir -p /opt/s3lm
  sudo tar -xzf /tmp/s3lm.tgz -C /opt/s3lm
  sudo chown -R \$USER /opt/s3lm
  cd /opt/s3lm/s3-live-monitor
  ls frontend/src >/dev/null && echo '  app tree ok'
  test -f ../intelligence/analysis/CASE-2026-CBTF/mule-accounts/extracted/unique-upi-list-full.txt && echo '  mule master ok'
  test -f ../LE-SUBMISSION-MASTER/17-CRYPTO-WALLETS.csv && echo '  wallet master ok'
"

# 6. build & run on VM
echo "→ building & starting on VM (this takes a few minutes)…"
gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command="
  set -e
  cd /opt/s3lm/s3-live-monitor
  # install docker if startup script hasn't finished
  if ! command -v docker >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq docker.io docker-compose-plugin
    sudo systemctl enable --now docker
  fi
  sudo docker compose up -d --build
  echo '--- containers ---'
  sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  sleep 6
  curl -s -m 10 http://127.0.0.1/api/healthz || curl -s -m 10 http://127.0.0.1:80/api/healthz || echo 'health check pending…'
"

echo ""
echo "✅ Deployed:  http://$IP/"
echo "   Health:    http://$IP/api/healthz"
echo "   Access key (if gated):  ssh $VM -- cat /root/.local/share/s3-live-monitor/access-token.txt  (or S3LM_TOKEN env)"
echo "   Logs:      gcloud compute ssh $VM -- sudo docker logs -f s3lm"
echo "   Stop VM when idle to save cost:  gcloud compute instances stop $VM --zone $ZONE --project $PROJECT"

# 7. post-deploy verification (added 2026-09-02: sync daemon + staleness fixes)
echo "→ post-deploy verification…"
gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command="
  cd /opt/s3lm/s3-live-monitor
  echo '--- containers ---'
  sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
  echo '--- s3sync daemon logs (first 15 lines) ---'
  sudo docker logs s3sync 2>&1 | head -15
  echo '--- s3lm health ---'
  curl -s -m 10 http://127.0.0.1/api/healthz && echo
"
