#!/usr/bin/env bash
set -euo pipefail
# Bootstrap S3 Live Monitor from GitHub on a 4-vCPU GCP VM, then deploy.
# Unlike the old tarball flow this ships NO reference data and NO big context:
# the VM clones the private repo with a read-only deploy key, and every later
# change goes through ./deploy/deploy.sh (git pull -> cached docker build).
#
# Usage:  ./deploy/gcp-deploy.sh [vm-name]     (default: s3lm-budget)
# Env:    GCP_PROJECT GCP_ZONE GCP_MACHINE DISK_GB
#         GITHUB_TOKEN=<token with repo scope>  (needed once, to register the deploy key)
#         GIT_REPO=owner/repo  (default vijaykavuri43-sudo/cricketbetting)
PROJECT="${GCP_PROJECT:-learn2earn-4f6a5}"
VM="${1:-s3lm-budget}"
ZONE="${GCP_ZONE:-asia-south1-a}"
REGION="${GCP_REGION:-asia-south1}"
MACHINE="${GCP_MACHINE:-e2-standard-4}"
DISK_GB="${DISK_GB:-40}"
GIT_REPO="${GIT_REPO:-vijaykavuri43-sudo/cricketbetting}"
CLONE_URL="git@github.com:${GIT_REPO}.git"
APP=/opt/s3lm/s3-live-monitor
DEPLOY_KEY="$HOME/.ssh/s3lm_github_deploy"

echo "-> Project: $PROJECT  VM: $VM  Zone: $ZONE  Machine: $MACHINE  Disk: ${DISK_GB}GB"
echo "-> Source: $CLONE_URL  (git clone - no tarball)"

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
  apt-get install -y -qq docker.io docker-compose-plugin git
  systemctl enable --now docker
fi
if ! command -v git >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq git
fi
# 2GB emergency swap
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null || true
fi
' \
  echo "  VM $VM created - waiting for startup script..."
  sleep 45
else
  STATE=$(gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" --format='get(status)')
  if [ "$STATE" != "RUNNING" ]; then
    echo "  VM $VM is $STATE - starting..."
    gcloud compute instances start "$VM" --project="$PROJECT" --zone="$ZONE" --quiet
    sleep 20
  else
    echo "  VM $VM already running"
  fi
fi

# 4. wait for SSH
echo -n "  waiting for SSH"
for i in $(seq 1 30); do
  if gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --command="echo ok" --quiet 2>/dev/null | grep -q ok; then echo " - ready"; break; fi
  echo -n "."; sleep 4
done

IP=$(gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
echo "  VM IP: $IP"

# 5. GitHub read-only deploy key (generated once, reused if present)
if [ ! -f "$DEPLOY_KEY" ]; then
  echo "-> generating GitHub deploy key..."
  ssh-keygen -t ed25519 -f "$DEPLOY_KEY" -N "" -C "s3lm-$VM" >/dev/null 2>&1
  echo "  created $DEPLOY_KEY"
else
  echo "-> reusing existing deploy key $DEPLOY_KEY"
fi
PUB=$(cat "${DEPLOY_KEY}.pub")

if [ -n "${GITHUB_TOKEN:-}" ]; then
  echo "-> registering deploy key on $GIT_REPO..."
  # idempotent: 422 already-exists is fine
  HTTP=$(curl -s -o /tmp/s3lm-key-resp.json -w '%{http_code}' \
    -H "Authorization: token $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$GIT_REPO/keys" \
    -d "{\"title\":\"s3lm-$VM-deploy\",\"key\":\"$PUB\",\"read_only\":true}")
  if [ "$HTTP" != "201" ] && ! grep -q "key is already in use" /tmp/s3lm-key-resp.json 2>/dev/null; then
    echo "  ! could not register key (HTTP $HTTP): $(cat /tmp/s3lm-key-resp.json | head -c 200)" >&2
  else
    echo "  deploy key registered (or already present)"
  fi
else
  echo "WARN GITHUB_TOKEN unset - skipping key registration on GitHub."
  echo "  If the key was never added, clone will fail; register it manually:"
  echo "    gh repo deploy-key add \"${DEPLOY_KEY}.pub\" -R $GIT_REPO --allow-write=false"
fi

# 6. copy the deploy key to the VM and clone/pull the repo
echo "-> cloning/pulling app from GitHub on VM..."
gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command="
set -e
mkdir -p ~/.ssh && chmod 700 ~/.ssh
grep -q 's3lm_github_deploy' ~/.ssh/config 2>/dev/null || cat >> ~/.ssh/config <<'EOC'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/s3lm_github_deploy
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOC
"
# send key bytes over stdin to avoid enlarging the command line with a full key
cat "$DEPLOY_KEY" | gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command="cat > ~/.ssh/s3lm_github_deploy && chmod 600 ~/.ssh/s3lm_github_deploy"

gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command="
set -e
# tarball-era backups are stale - remove them all
rm -rf /opt/s3lm/s3-live-monitor.previous-* 2>/dev/null || true
mkdir -p /opt/s3lm/intelligence/analysis /opt/s3lm/intelligence/audit /opt/s3lm/intelligence/exports /opt/s3lm/intelligence/registry
if [ ! -d $APP/.git ]; then
  rm -rf $APP
  git clone $CLONE_URL $APP
else
  cd $APP && git fetch origin && git reset --hard origin/main
fi
cd $APP
test -f docker-compose.yml && echo '  git clone ok'
"

# 7. no reference data shipped (app reads GCS/S3 bucket data only)
echo "-> no reference data shipped (app reads GCS/S3 bucket only)"
gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --quiet --command="
set -e
cd $APP
sudo docker compose up -d --build
sleep 6
echo '--- containers ---'
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
echo '--- health ---'
curl -fsS -m 10 http://127.0.0.1/api/healthz && echo || curl -fsS -m 10 http://127.0.0.1:80/api/healthz && echo
"

echo ""
echo "OK Bootstrapped:  http://$IP/"
echo "   Health:        http://$IP/api/healthz"
echo "   Daily deploy:  ./deploy/deploy.sh $VM"
echo "   Access key (if gated):  ssh $VM -- cat /root/.local/share/s3-live-monitor/access-token.txt  (or S3LM_TOKEN env)"
echo "   Logs:      gcloud compute ssh $VM -- sudo docker logs -f s3lm"
echo "   Stop VM when idle to save cost:  gcloud compute instances stop $VM --zone $ZONE --project $PROJECT"