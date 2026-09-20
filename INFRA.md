# s3-live-monitor — Infrastructure Reference (CANONICAL)

> **AI assistants: read this file FIRST when asked to log in to the VM, check the
> dashboard, deploy, or touch anything in GCP.** Do not guess project/zone/bucket
> names — they are all listed below. This file is the single source of truth; if a
> script default disagrees with it, this file wins (and the script is a bug — fix it).

## 1. The dashboard VM (where the app runs)

| Item | Value |
|------|-------|
| VM name | `s3lm-budget` |
| **GCP project** | `project-9641a7b9-3759-4c98-9b8` |
| Zone | `asia-south1-a` |
| Machine / disk | `t2d-standard-8` (8 vCPU, 32 GB) · 100 GB boot disk (~21% used) |
| External IP | `8.231.108.145` (ephemeral — re-check with `gcloud`, do not hardcode) |
| Dashboard URL | `http://8.231.108.145/` (re-check IP first) — bucket 1 `bx-s3-dev-001` |
| Bucket-2 dashboard | `http://8.231.108.145:81/` — `bxs3dev001` (690K keys, deposit/withdrawal slips) |
| Bucket-3 dashboard | `http://8.231.108.145:82/` — `tw-artifacts-center` (10K keys, QR/proofs/brand themes) |
| Firewall | `allow-s3lm-extra-dashboards` (tcp:81,82, 0.0.0.0/0) + `default-allow-http` (tcp:80) |
| Health endpoint | `http://<IP>[:port]/api/healthz` (public, no auth) |
| App dir on VM | `/opt/s3lm/s3-live-monitor` (git clone of this repo, branch `main`) |
| Containers | `s3lm` (dash :80→8787) · `s3sync` (mirror worker b1) · `s3lm-b2` (:81) · `s3sync-b2` · `s3lm-b3` (:82) · `s3sync-b3` |
| Dashboard auth | SINGLE shared key (2026-09-20): all three dashboards (`s3lm`, `s3lm-b2`, `s3lm-b3`) accept the primary key — read it via `docker exec s3lm cat /root/.local/share/s3-live-monitor/access-token.txt` **on the VM**. (b2/b3 token files in their volumes were overwritten with the primary key; volumes persist across deploys so it survives redeploys. To rotate: write new key to all three files + restart.) Every `/api` route except `/api/healthz` needs `Authorization: Bearer <key>` |
| Watchdog | `/usr/local/bin/s3lm-watchdog.sh` via cron `/etc/cron.d/s3lm-watchdog` (restarts `s3lm` only after 3 consecutive failed checks; logs to syslog as `s3lm-watchdog`) |

## 2. SSH — copy-paste (ALWAYS pass `--project` and `--zone`)

```bash
# Interactive login
gcloud compute ssh s3lm-budget \
  --project=project-9641a7b9-3759-4c98-9b8 \
  --zone=asia-south1-a

# One-shot command (non-interactive — use this from agent shells)
gcloud compute ssh s3lm-budget \
  --project=project-9641a7b9-3759-4c98-9b8 \
  --zone=asia-south1-a --quiet \
  --command="sudo docker ps --format 'table {{.Names}}\t{{.Status}}'"
```

> ⚠️ The local gcloud default project is `learn2earn-4f6a5` — **that is NOT where
> this VM lives.** Every `gcloud` command for this VM MUST include
> `--project=project-9641a7b9-3759-4c98-9b8 --zone=asia-south1-a`.
> `deploy/gcp-deploy.sh` still defaults to the old project — pass
> `GCP_PROJECT=project-9641a7b9-3759-4c98-9b8` explicitly if you ever bootstrap.

## 3. Buckets / data sources (where evidence comes from)

| Item | Value |
|------|-------|
| **GCS evidence bucket** | `cbtf-case-evidence-518405669914` |
| Mirror prefix b1 (`bx-s3-dev-001`) | `gs://cbtf-case-evidence-518405669914/s3-mirror/` + status `s3-mirror-sync/` |
| Mirror prefix b2 (`bxs3dev001`) | `gs://…/s3-mirror-bxs3dev001/` + status `s3-mirror-bxs3dev001-sync/` |
| Mirror prefix b3 (`tw-artifacts-center`) | `gs://…/s3-mirror-tw-artifacts/` + status `s3-mirror-tw-artifacts-sync/` |
| Upstream public S3 buckets | `https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com` · `https://bxs3dev001.s3.ap-southeast-1.amazonaws.com` · `https://tw-artifacts-center.s3.ap-southeast-1.amazonaws.com` |
| State DBs on VM | `s3lm-data` → `manifest.db` (b1) · `s3lm-data-b2` (b2) · `s3lm-data-b3` (b3); sync state `s3sync-state[-b2/-b3]` |

Defaults live in `docker-compose.yml` (`S3LM_GCS_BUCKET`, `S3SYNC_GCS_BUCKET`) and
`backend/config.py`. The VM uses compose defaults (no `.env` overrides on the VM).

> ⚠️ `S3LM_PUBLIC_URL` (default `http://8.231.108.145`, set in compose) is baked
> into officer-verifiable exhibit links in CSV exports. If the VM is ever
> recreated with a **new external IP**, update `S3LM_PUBLIC_URL` and redeploy —
> otherwise exhibits link to a dead address.

## 4. Deploy (after ANY code change)

```bash
cd s3-live-monitor
./deploy/ship.sh "what changed"   # commit → push → VM: git pull + cached rebuild + health check
./deploy/ship.sh --vm s3lm-budget "msg"  # explicit VM (same default)
```

Rules: fix locally → run `pytest` (148 tests must pass) → `ship.sh` → verify
`http://<IP>/api/healthz` is 200 and `docker ps` shows both containers healthy.
Never edit code directly on the VM — the next deploy (`git reset --hard`) wipes it.

## 5. Quick triage cheat-sheet (on the VM)

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'   # 6 containers healthy
curl -s -m 10 http://127.0.0.1/api/healthz && echo         # :80 (b1)
curl -s -m 10 http://127.0.0.1:81/api/healthz && echo      # :81 (b2)
curl -s -m 10 http://127.0.0.1:82/api/healthz && echo      # :82 (b3)
grep 's3lm-watchdog' /var/log/syslog | tail -n 5           # watchdog restarts
sudo docker logs --since 10m s3lm 2>&1 | tail -n 30        # app logs
sudo docker logs --since 10m s3sync 2>&1 | tail -n 30      # mirror-worker logs
sudo docker logs --since 10m s3sync-b2 2>&1 | tail -n 30   # b2 mirror logs
sudo docker logs --since 10m s3sync-b3 2>&1 | tail -n 30   # b3 mirror logs
```

## 6. Change log (keep this updated when infra moves)

- 2026-09-20: 3-bucket monitoring live. Added `s3lm-b2` (:81, `bxs3dev001`,
  690K keys deposit/withdrawal slips, slip-OCR on, 2 enrich workers, 10G cap)
  + `s3sync-b2` (→ `s3-mirror-bxs3dev001/`) and `s3lm-b3` (:82,
  `tw-artifacts-center`, 10K keys QR-images/payment-proofs/brand themes,
  QR+OCR prefix overrides, 6G cap) + `s3sync-b3`
  (→ `s3-mirror-tw-artifacts/`). Backend env-driven per-source config
  (`S3LM_SOURCE_LABEL`, `S3LM_SYNC_STATUS_PREFIX`, `S3LM_PEERS`,
  `S3LM_{HOT,QR,OCR,DEFAULT}_PREFIXES`, `S3LM_DOWNLOADS_SUBDIR`); sidebar
  Sources switcher; per-dashboard gate tokens. Firewall
  `allow-s3lm-extra-dashboards` (tcp:81,82). 198 pytest pass. Both new
  mirrors syncing LIVE, errors=0 (b2 catch-up ~4 obj/s over 690K keys).

- 2026-09-17: canonical doc created; verified live values (VM RUNNING, both
  containers healthy, bucket `…-518405669914` confirmed from running containers).
- 2026-09-17: fixed stale default bucket `cbtf-case-evidence-991096649643` in
  `sync/s3-gcs-sync.py` → `cbtf-case-evidence-518405669914` (env already overrode
  it on the VM; the default was a landmine).
- 2026-09-17: stale-ref sweep — `deploy/gcp-deploy.sh` defaults corrected
  (project `learn2earn-4f6a5`→`project-9641a7b9-3759-4c98-9b8`, machine
  `e2-standard-4`→`t2d-standard-8`, disk 40→100 GB); `docker-compose.yml` host
  comment `e2-standard-8`→`t2d-standard-8`; `S3LM_PUBLIC_URL` centralized in
  compose; plan docs (`DASHBOARD-FIX-PLAN.md`, `MONITORING-IMPROVEMENT-PLAN.md`)
  corrected to live bucket/machine/disk.
