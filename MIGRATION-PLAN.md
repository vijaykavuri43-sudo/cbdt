# Migration Plan — s3-live-monitor to the `v2660228@gmail.com` account

> **Status: COMPLETE — all old-account GCP projects moved, no data copy.** On 2026-10-25
> the main project `project-9641a7b9-3759-4c98-9b8` and every remaining old-account
> project (`gen-lang-client-0791146751`, `project-9dd84106-d3f1-4f89-b4e`,
> `s3sync-508116`) were moved into `v2660228-org` (237163885821). The main project ID,
> VM, static IP `8.231.108.145`, GCS bucket, and evidence are unchanged. The old billing
> account now has zero projects; both old orgs are empty and pending Console deletion.
> See “Execution status” below.
>
> Measured 2026-10-24 ~18:00 UTC; main move 2026-10-25 01:01 UTC; remaining moves
> 2026-10-25 15:18–15:24 UTC.
>
> **Reusable runbook:** see
> [`GCP-ACCOUNT-MIGRATION-GUIDE.md`](GCP-ACCOUNT-MIGRATION-GUIDE.md) for the
> step-by-step commands, role matrix, org-policy gotchas, verification,
> security cleanup, rollback, and case study.

---

## 0. Target and approach

| | Source (today) | Destination |
|---|---|---|
| Google account | `vijaykavuri72@gmail.com` (old owner) | `v2660228@gmail.com` (new owner) |
| Project | `project-9641a7b9-3759-4c98-9b8` (“My First Project”, #518405669914) | **same project**, now parented to `v2660228-org` (237163885821) |
| Billing | `01B553-891570-5D2D90` — **CLOSED** | `01A4FE-5BE581-D29766` — **OPEN**, INR |
| VM | `s3lm-budget`, `t2d-standard-8`, 100 GB `pd-balanced`, `asia-south1-a`, static IP `s3lm-budget-ip` = `8.231.108.145` | **same VM**, same IP, same zone |
| GCS evidence bucket | `cbtf-case-evidence-518405669914` (`ASIA-SOUTH1`, STANDARD, UBLA, soft-delete 7d) | **same bucket**, same project |

**Actual approach:** Strategy 1 — transfer the project itself. No new VM, no bucket copy,
no data migration. The project was moved between organizations; every resource stayed in
place. The original fresh-rebuild/Strategy 2 plan is retained below for reference only.

### Can the bucket itself be “moved” to the other account?

**No — not the bucket alone.** Google Cloud makes a bucket's **parent project immutable**:
*“some of the properties you set are permanent and cannot be changed, including the
bucket's name and its parent project.”* Bucket relocation (`gcloud storage buckets
relocate`) only changes the **location**, not the project. There are exactly two ways to
end up with the data under `v2660228@gmail.com`:

| Strategy | How | Copy volume | Time / cost | Trade-off |
|---|---|---|---|---|
| **1. Transfer the whole project** *(used)* | Link billing to the open account, move the project into `v2660228-org`, remove the old account's roles | **None** | minutes / $0 | Project ID and bucket name stay `…518405669914`; account control is org/IAM-based, not a rename |
| **2. Copy the bucket** | Create a new bucket in `project-9e3226b0…`, use Storage Transfer Service or `gcloud storage rsync` | ~879 GB / ~2.99 M objects | 2–8 h / ~$15–30 ops + double storage during overlap | Clean new project ID, but a long copy and a second full listing to verify |

**Recommendation: Strategy 1.** It is zero-copy, cannot miss an object, and avoids hours
of transfer while the source billing is broken. Choose Strategy 2 only if the new project
ID is itself a requirement. The phases below describe Strategy 2; **skip Phase 3.1 if you
choose Strategy 1** — the VM/volume work is identical, and the new VM simply gets IAM
access to the existing bucket.

**What is *not* migrated:** the three AWS S3 buckets (`bx-s3-dev-001`, `bxs3dev001`,
`tw-artifacts-center`) are public upstream sources we do not own. What must be preserved
is the **GCS mirror** (~879 GB, ~2.99 M objects), including objects that have since been
deleted from the upstream S3 buckets — that is the evidentiary copy.

---

## Execution status — 2026-10-25 (COMPLETE)

### Final state

- **All four old-account projects are now under `organizations/237163885821`
  (`v2660228-org`):**
  - `project-9641a7b9-3759-4c98-9b8` (#518405669914) — main app project; moved
    2026-10-25 01:01 UTC.
  - `gen-lang-client-0791146751` (#511645898757) — Gemini API project; moved
    2026-10-25 15:19 UTC. Its Gemini API key and three `ais-gemini-key-*` service
    accounts are intact.
  - `project-9dd84106-d3f1-4f89-b4e` (#532069119571) — empty default project; moved
    2026-10-25 15:24 UTC.
  - `s3sync-508116` (#1022063185539) — empty project; moved 2026-10-25 15:18 UTC.
- The main project's ID/number, VM `s3lm-budget`, static IP `8.231.108.145`, boot disk,
  Docker volumes, `/opt/s3lm/intelligence`, GCS bucket
  `cbtf-case-evidence-518405669914`, and all evidence objects are unchanged. VM is
  **RUNNING**; `/api/healthz` returns **200**; bucket read/write was verified as
  `v2660228`.
- **Billing:** old `01B553-891570-5D2D90` is CLOSED and now has **zero linked
  projects**. New `01A4FE-5BE581-D29766` is OPEN and linked to the main project,
  `gen-lang-client-0791146751`, and the new account's own
  `project-9e3226b0-2632-488c-b87` (#114849318697, not from the old account).
  `project-9dd84106...` and `s3sync-508116` are billing-disabled because the new
  billing account hit its project-link quota; they contain no resources. Request a
  quota increase at `https://support.google.com/code/contact/billing_quota_increase`
  if those two need billing later.
- **Old account access (verified 2026-10-25):** `vijaykavuri72` has **zero
  projects**; all four moved projects deny access; the account is not in bucket
  IAM, new-org IAM, VM service-account IAM, or project/instance SSH-key metadata.
  Its local gcloud credential was revoked. It still has Organization Owner roles
  on the two empty old orgs because that is required to delete them.
- **Old orgs:** both `755663317` and `1052014037805` have **zero projects and zero
  folders**. They are still ACTIVE under `vijaykavuri72` and must be deleted in the
  Console. Deletion is Console-only, 30-day soft delete. Remove the pending
  `v2660228` owner invitation on 755663317 first if it still appears (Actions →
  Remove).
- **New org:** `v2660228-org` (237163885821) contains five projects: the four moved
  plus the new account's own `project-9e3226b0...`. `v2660228` has Organization
  Admin, Project Creator, Project Mover, Billing Admin, and Org Policy Admin on the
  new org; Editor, Project IAM Admin, Billing Project Manager, and Project Mover on
  each moved project.
- **Old billing debt:** `01B553-891570-5D2D90` remains CLOSED and its outstanding
  balance is still owed separately. Closing the old Google account or deleting the
  old orgs does not clear that debt.
- **Do not close the old Google account yet:** delete both old orgs in the Console
  first, verify the old account's project list is empty, then close it. The old
  account is the only principal that can delete its standalone orgs.
- **Metadata backup:** `migration-backup/old-account-metadata/` contains pre-move
  project/org/billing JSON, IAM, enabled services, service accounts, and API-key
  metadata. The main VM/bucket backups remain as listed below.

### Backups retained

- Snapshot `s3lm-pre-migration-20261024` — READY (100 GB disk, ~12.1 GB stored).
- GCS `gs://cbtf-case-evidence-518405669914/_migration-backup/`:
  `app-and-intelligence.tgz` (772,603,031 bytes),
  `decoded-dbs-2026-10-24.tgz` (1,762,067,621 bytes), `SHA256SUMS`.
- Local `migration-backup/small-critical.tgz` — SHA256
  `c3aa4b323a9477d6fe7a2440ad56010a129ea68c36d62a05acf00b5a10f39761`.
- No 879 GB copy was made. The bucket never moved.

### What was tried (and why the project move was used)

- The standalone-org owner invitation was sent successfully and showed **Pending**,
  but accepting it as `v2660228` failed with
  `This owner cannot be accepted to the organisation` (tracking
  `c5343937694843540`). The old org policy list showed no custom owner constraint.
- `v2660228` already owns its own standalone org (`v2660228-org`), so the cleaner
  path was to move the existing project into that org. This required:
  - Project Mover on the source org (granted to `v2660228`).
  - Project Creator on the destination org (already held).
  - `roles/orgpolicy.policyAdmin` on both orgs (temporarily granted).
  - `allowedExportDestinations` on the old org and `allowedImportSources` on the new
    org, using the `under:organizations/...` value format.
- After propagation, the move completed successfully. The old org-owner invitation
  is no longer needed.
- The three remaining old-account projects were moved the same way on 2026-10-25:
  `s3sync-508116` and `gen-lang-client-0791146751` were standalone and needed
  `roles/resourcemanager.projectIamAdmin` on each project; `project-9dd84106...` was
  in old org `1052014037805` and also needed temporary
  `roles/resourcemanager.projectMover` on that source org. The allowlist values had
  to use the `under:organizations/ORG_ID` format. All temporary roles and policies
  were removed after the moves.

---

## 1. Measured inventory (source of truth for acceptance)

### 1.1 VM

| Item | Value |
|---|---|
| Boot disk used | 40 GB of 100 GB (42%) |
| App checkout | `/opt/s3lm/s3-live-monitor`, HEAD `64b015a`, **63 modified files uncommitted** |
| Bind mount | `/opt/s3lm/intelligence` — **3.4 GB** (analysis 3.4 G, audit 8.2 M, exports 7.3 M, registry 1 M) |
| `.env` | 149 B, contains `S3LM_GEMINI_API_KEY`, `S3LM_GROQ_API_KEY` (secrets) |
| Containers | `s3lm`, `s3sync`, `s3lm-b2`, `s3sync-b2`, `s3lm-b3`, `s3sync-b3` — all healthy |
| Exposed | `:80` (b1), `:81` (b2), `:82` (b3), `:8443` TLS (`socat`) |
| TLS cert | `/etc/s3lm-le.pem` for `8-231-108-145.nip.io` — **tied to the old IP** |
| Extra host config | `/usr/local/bin/s3lm-watchdog.sh` + `/etc/cron.d/s3lm-watchdog`; `/usr/local/bin/backup_state_db.sh` + `/etc/cron.d/s3lm-db-backup`; `/etc/systemd/system/s3lm-tls.service`; `certbot.timer` |

### 1.2 Active Docker volumes

| Volume | Size | Main DB |
|---|---:|---:|
| `s3-live-monitor_s3lm-data` | 9.3 GB | `manifest.db` 8.5 GB |
| `s3-live-monitor_s3lm-data-b2` | 2.8 GB | `manifest.db` 2.6 GB |
| `s3-live-monitor_s3lm-data-b3` | 364 MB | `manifest.db` 271 MB |
| `s3-live-monitor_s3sync-state` | 346 MB | `state.db` 342 MB |
| `s3-live-monitor_s3sync-state-b2` | 158 MB | `state.db` 154 MB |
| `s3-live-monitor_s3sync-state-b3` | 24 MB | `state.db` 20 MB |
| **Total** | **~13.0 GB** | |

Stale volumes `s3lm_s3lm-data` (777 MB) and `s3lm-data` (301 MB) are **not** used by the
current compose project and must **not** be migrated.

### 1.3 DB baseline (captured 2026-10-24)

| DB | Rows | Bytes |
|---|---:|---:|
| b1 `manifest.db` → `objects` | 2,191,554 | 694,509,071,100 |
| b2 `manifest.db` → `objects` | 731,278 | 176,965,750,701 |
| b3 `manifest.db` → `objects` | 65,906 | 7,818,660,601 |
| b1 `state.db` → `synced` | 2,191,545 | 694,509,101,719 |
| b2 `state.db` → `synced` | 731,278 | 176,965,750,701 |
| b3 `state.db` → `synced` | 65,906 | 7,818,660,601 |

b1 `sync_failures` pending = 3; b3 terminal = 31 (expected; preserve).
b1 state is 9 rows behind manifest (in-flight); b3 is 0 behind.

### 1.4 GCS bucket prefixes

`s3-mirror/`, `s3-mirror-bxs3dev001/`, `s3-mirror-tw-artifacts/`,
`s3-mirror-sync/`, `s3-mirror-bxs3dev001-sync/`, `s3-mirror-tw-artifacts-sync/`,
`_backups/`, `manifests/`, `_test/`.

Copy **all** prefixes. No versioning; soft-delete 7 days. Do not use `--delete-*`.

---

## Phase 0 — restore billing (hard blocker)

Evidence:
- `gcloud storage ls gs://…/s3-mirror/` → `403 The billing account … is disabled in state delinquent`.
- GCS object download from inside `s3lm` → same 403.
- `gcloud compute snapshots create` → `BILLING_DISABLED`.
- `s3sync` logs: `DEGRADED — listing only, uploads paused`; last successful sync
  `2026-10-23T23:23:13Z`. Bucket **metadata** listing still works; data plane does not.
- Old project lifecycle is still `ACTIVE`; the closed billing account is
  `01B553-891570-5D2D90` (`open: false`, INR).

### Option A — link the old project to the new OPEN billing account (recommended)

Both accounts are INR, so linking should be accepted. This restores the old project
immediately and keeps billing under the destination account.

> **You do not have to clear the old balance first.** Google's documented recovery path for
> a project on a closed billing account is to *"move the projects to a different, active
> Cloud Billing account"* (or reopen the old one). The outstanding balance stays with the
> old account; it does not block the relink. Requirements: **Project Owner** on the old
> project + **Billing Account Admin / Project Manager** on the destination account. The old
> debt can still go to collections, so settle it separately when you can.

1. As **v2660228**, grant `vijaykavuri72@gmail.com` billing admin on
   `01A4FE-5BE581-D29766` (Console → Billing → Account management → Permissions →
   Add principal → role **Billing Account Administrator**).
   If IAM API accepts it:
   ```bash
   gcloud billing accounts add-iam-policy-binding 01A4FE-5BE581-D29766 \
     --member=user:vijaykavuri72@gmail.com \
     --role=roles/billing.admin \
     --account=v2660228@gmail.com
   ```
2. As **vijaykavuri72**, re-link the project:
   ```bash
   gcloud billing projects link project-9641a7b9-3759-4c98-9b8 \
     --billing-account=01A4FE-5BE581-D29766 \
     --account=vijaykavuri72@gmail.com
   ```
3. Verify (must all succeed before continuing):
   ```bash
   gcloud billing projects describe project-9641a7b9-3759-4c98-9b8 \
     --account=vijaykavuri72@gmail.com          # billingEnabled: true
   gcloud storage ls gs://cbtf-case-evidence-518405669914/_test/ \
     --account=vijaykavuri72@gmail.com          # no 403
   gcloud compute snapshots create s3lm-billing-test \
     --source-disk=s3lm-budget --source-disk-zone=asia-south1-a \
     --project=project-9641a7b9-3759-4c98-9b8 --account=vijaykavuri72@gmail.com
   ```
   Delete the test snapshot after it reaches `READY`.

### Option A2 — then transfer project ownership (Strategy 1, zero-copy)

With billing active, hand the whole project to the destination account. The bucket, VM,
disk, static IP, and all data stay exactly where they are.

1. As **vijaykavuri72**, add the destination account as project Owner:
   ```bash
   gcloud projects add-iam-policy-binding project-9641a7b9-3759-4c98-9b8 \
     --member=user:v2660228@gmail.com \
     --role=roles/owner \
     --account=vijaykavuri72@gmail.com
   ```
2. Sign in as **v2660228** and accept the Owner invitation if the Console shows one
   (IAM & Admin → IAM). Owner grants to a different Google account may arrive by email.
3. Verify as **v2660228**:
   ```bash
   gcloud projects describe project-9641a7b9-3759-4c98-9b8 \
     --account=v2660228@gmail.com
   gcloud storage ls gs://cbtf-case-evidence-518405669914/ \
     --account=v2660228@gmail.com
   gcloud compute instances list \
     --project=project-9641a7b9-3759-4c98-9b8 \
     --account=v2660228@gmail.com
   ```
4. Only after verification, remove the old account's Owner binding (and any other
   roles you want revoked):
   ```bash
   gcloud projects remove-iam-policy-binding project-9641a7b9-3759-4c98-9b8 \
     --member=user:vijaykavuri72@gmail.com \
     --role=roles/owner \
     --account=v2660228@gmail.com
   ```
5. The project ID and bucket name still read `…518405669914`. That is cosmetic — the
   account in control and the billing account are now `v2660228`.

> Google documents the billing-change permission set as **Project Owner + Billing Account
> User + Billing Account Viewer** on *both* the current and target billing accounts (or
> Billing Account Administrator on both). The closed account is still a valid billing
> account object; if unlinking from it is refused, reopen it first (Option B) or ask
> support to move the project.

> Note: `gcloud billing accounts get-iam-policy` currently returns `INVALID_ARGUMENT`.
> Use the Console to confirm/assign the billing role if the gcloud IAM command fails.

### Option B — reopen the closed billing account

Console → Billing → Account management → `01B553-891570-5D2D90` → **Reopen**. Possible
within 30 days of closure and after clearing any outstanding balance/payment method.
Verify with `gcloud billing accounts describe 01B553-891570-5D2D90` → `open: true`, then
run the same three verification commands above.

### Option C — support escalation

If neither A nor B works, open a **billing/account** support case to restore access.
**Do not delete the project or bucket.** Until access is restored, every hour of
sync downtime increases the window in which an object deleted upstream can be missed.

---

## If you cannot pay yet — pause strategy (read before waiting)

There is **no pause button for a project or a bucket**. The only large saving is to stop
the VM; storage, disk, and the static IP keep costing. And a closed billing account is
**not a safe parking state**: Google's ~30-day recovery window applies, and resources can
be deleted even inside it.

### What can and cannot be paused

| Resource | Action | Running | Paused | Notes |
|---|---|---|---|---|
| VM `s3lm-budget` (vCPU/RAM) | **Stop**, don't suspend | ~$250–330/mo | **$0** | Restart needs active billing again |
| 100 GB `pd-balanced` boot disk | Keep | ~$10/mo | ~$10/mo | Deleting it loses the VM state |
| Static IP `s3lm-budget-ip` | Keep (or release) | ~$3.65/mo | ~$3.65/mo | Charged while associated even if stopped; releasing loses `8.231.108.145` |
| GCS bucket (~879 GB) | **Cannot pause** | ~$20/mo | ~$20/mo | Deleting it destroys the evidence |
| Snapshot (if present) | Keep | ~$1–2/mo | ~$1–2/mo | Cheap rollback anchor |
| **Total** | | **~$285–365/mo** | **~$35/mo** | |

- Stop from **inside** the VM if the API is blocked by billing: `sudo poweroff`.
  The containers stop with it. You won't be able to start it again until billing is active.
- The static IP is billed at the in-use rate even when the VM is stopped: Google considers
  it *"in use if it is associated with a VM instance whether the instance is running or
  stopped."*
- GCS has no pause. The only way to stop storage charges is to delete the objects — which
  destroys the evidence. **Do not delete the bucket.**
- Moving 879 GB to Coldline/Archive would cut storage to a few dollars/month, but requires
  rewriting ~3M objects (Class A ops, roughly $150+), has 90/365-day minimums and retrieval
  fees, and can't run while billing is disabled. Not a practical rescue.

### The cheapest safe path

1. **Run Phase 0.5 now** — while SSH still works. Once the VM is stopped you can't copy
   anything off it, and restarting may require billing.
2. **Move the project to the new open billing account** (Option A) — you don't have to pay
   the old bill first. This restores GCS access and stops the deletion clock.
3. **Stop the VM** — leaves ~$35/mo (disk + IP + GCS) instead of ~$300/mo.
4. Check whether the new account has **free-trial credits**; if it does, they may cover the
   ~$35/mo for months.
5. If you truly cannot cover ~$35/mo, the only options are to find the money or accept that
   the GCS evidence may be deleted. There is no free way to keep 879 GB.

### Deadline

Treat the closed-account grace period as **~30 days from closure**. Google states resources
may be removed and become non-recoverable while billing is disabled, and the 30-day project
recovery window does not guarantee every resource. Do not wait months.

---

## Phase 0.5 — emergency on-disk copy (run NOW; billing not required)

SSH and disk reads still work while billing is blocked. Copy the only irreplaceable
**decoded** state off the VM as insurance. This is **not** a substitute for the GCS copy:
the ~879 GB of raw object evidence still requires Phase 0. But it preserves the decoded
manifest DBs (the expensive 200h+ decode), sync state, audit/analysis outputs, the
uncommitted code, and `.env`.

The stack can stay live — `VACUUM INTO` produces a consistent SQLite snapshot.

```bash
# On the old VM (as prism, passwordless sudo)
sudo mkdir -p /tmp/emergency

# consistent DB snapshots (same technique as backup_state_db.sh)
for spec in \
  "s3lm:/root/.local/share/s3-live-monitor/manifest.db:manifest-b1.db" \
  "s3lm-b2:/root/.local/share/s3-live-monitor/manifest.db:manifest-b2.db" \
  "s3lm-b3:/root/.local/share/s3-live-monitor/manifest.db:manifest-b3.db" \
  "s3sync:/root/.local/share/s3-sync/state.db:state-b1.db" \
  "s3sync-b2:/root/.local/share/s3-sync/state.db:state-b2.db" \
  "s3sync-b3:/root/.local/share/s3-sync/state.db:state-b3.db"; do
  c=${spec%%:*}; rest=${spec#*:}; db=${rest%%:*}; out=${rest##*:}
  sudo docker exec "$c" python3 -c \
    "import sqlite3;sqlite3.connect('$db').execute(\"VACUUM INTO '/tmp/$out'\")"
  sudo docker cp "$c:/tmp/$out" "/tmp/emergency/$out"
  sudo docker exec "$c" rm -f "/tmp/$out"
done

# code (including 63 uncommitted files), intelligence outputs, secrets
sudo tar -C /opt/s3lm -czf /tmp/emergency/app-and-intelligence.tgz \
  s3-live-monitor intelligence
sudo tar -C /tmp/emergency -czf "/tmp/emergency/decoded-dbs-$(date -u +%F).tgz" \
  manifest-b1.db manifest-b2.db manifest-b3.db state-b1.db state-b2.db state-b3.db
sudo chown "$USER":"$USER" /tmp/emergency/*.tgz
sha256sum /tmp/emergency/*.tgz | tee /tmp/emergency/SHA256SUMS

# From the workstation: pull to local disk (local free space is ~865 GB)
mkdir -p ./migration-backup
# use cmd.exe gcloud on Windows; WSL-native gcloud hangs
gcloud compute scp --recurse \
  s3lm-budget:/tmp/emergency ./migration-backup/ \
  --project=project-9641a7b9-3759-4c98-9b8 --zone=asia-south1-a \
  --account=vijaykavuri72@gmail.com --quiet
```

Verify locally: `sha256sum -c migration-backup/emergency/SHA256SUMS`.
Compressed size is typically **5–8 GB** for the DBs + 3.4 GB intelligence tree.

> Caveat: decoded DBs contain the extracted payloads, not the original image/PDF bytes.
> Only the GCS mirror holds the raw evidence. Do not treat this as a completed migration.

---

## Phase 1 — backup anchor (immediately after billing is restored)

```bash
# 1. Full boot-disk snapshot (rollback anchor; read-only, no app impact)
gcloud compute snapshots create s3lm-pre-migration-20261024 \
  --source-disk=s3lm-budget --source-disk-zone=asia-south1-a \
  --project=project-9641a7b9-3759-4c98-9b8 \
  --account=vijaykavuri72@gmail.com \
  --description="pre-migration: VM state before move to v2660228"

# 2. Capture the uncommitted VM code (63 files!) — MUST NOT be lost
gcloud compute ssh s3lm-budget --project=project-9641a7b9-3759-4c98-9b8 \
  --zone=asia-south1-a --account=vijaykavuri72@gmail.com --quiet --command="
    cd /opt/s3lm/s3-live-monitor
    mkdir -p /tmp/mig
    git status --porcelain > /tmp/mig/vm-status.txt
    git diff > /tmp/mig/vm-uncommitted.patch
    git diff --stat > /tmp/mig/vm-diff-stat.txt
    sudo cp .env /tmp/mig/app.env
    sha256sum /tmp/mig/vm-uncommitted.patch /tmp/mig/app.env
  "

# 3. Download the patch + env locally and review/commit the patch BEFORE relying on
#    a fresh git clone. Anything not committed will not exist on the new VM.
gcloud compute scp --recurse \
  s3lm-budget:/tmp/mig ./migration-stage/ \  # path syntax varies; see note
  --project=project-9641a7b9-3759-4c98-9b8 --zone=asia-south1-a \
  --account=vijaykavuri72@gmail.com
```

> `gcloud compute scp` path syntax differs on Windows (`vm:C:\path` vs `vm:/path`). If it
> is awkward, stage the files in GCS (`_migration/`) instead — all later steps already use
> GCS staging.

Keep the snapshot until the new system has been signed off (≥ 14 days).

---

## Phase 2 — provision destination (old system keeps serving)

All commands run as `v2660228` on the **new** project unless stated.

### 4.1 Enable APIs, check quota

```bash
gcloud services enable compute.googleapis.com \
  --project=project-9e3226b0-2632-488c-b87 --account=v2660228@gmail.com

gcloud compute regions describe asia-south1 \
  --project=project-9e3226b0-2632-488c-b87 --account=v2660228@gmail.com \
  --format="yaml(quotas)" | grep -A4 -E "T2D_CPUS|CPUS|DISKS_TOTAL_GB|IN_USE_ADDRESSES"
```

If `T2D_CPUS` is 0, request a quota increase or use `e2-standard-8` (same 8 vCPU / 32 GB)
for the first boot; it can be changed later only with a stop/start.

### 4.2 Static IP + firewall

```bash
gcloud compute addresses create s3lm-ip --region=asia-south1 \
  --project=project-9e3226b0-2632-488c-b87 --account=v2660228@gmail.com

IP=$(gcloud compute addresses describe s3lm-ip --region=asia-south1 \
  --project=project-9e3226b0-2632-488c-b87 --account=v2660228@gmail.com \
  --format='value(address)')
echo "$IP"   # reserve this; it is the new dashboard address

gcloud compute firewall-rules create s3lm-web \
  --project=project-9e3226b0-2632-488c-b87 --account=v2660228@gmail.com \
  --network=default --direction=INGRESS --action=ALLOW \
  --rules=tcp:80,tcp:81,tcp:82,tcp:8443 \
  --source-ranges=0.0.0.0/0 --target-tags=s3lm-web
```

`default-allow-ssh` normally exists in a default VPC; create tcp:22 if not.

### 4.3 Destination bucket

```bash
gcloud storage buckets create gs://cbtf-case-evidence-114849318697 \
  --project=project-9e3226b0-2632-488c-b87 --account=v2660228@gmail.com \
  --location=asia-south1 \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --soft-delete-duration=7d

NEW_SA=$(gcloud compute project-info describe \
  --project=project-9e3226b0-2632-488c-b87 --account=v2660228@gmail.com \
  --format='value(defaultServiceAccount)')

gcloud storage buckets add-iam-policy-binding gs://cbtf-case-evidence-114849318697 \
  --member="serviceAccount:${NEW_SA}" --role=roles/storage.objectAdmin \
  --account=v2660228@gmail.com

# Temporary read on the source bucket for the new VM service account.
# (Requires Phase 0 to be complete.)
gcloud storage buckets add-iam-policy-binding gs://cbtf-case-evidence-518405669914 \
  --member="serviceAccount:${NEW_SA}" --role=roles/storage.objectViewer \
  --account=vijaykavuri72@gmail.com
```

### 4.4 Create the VM

Startup script must install: Docker + compose v2, git, `socat`, `certbot`,
Google Cloud CLI, 2 GB swap. Do **not** reuse the old script unchanged — it installs
`docker-compose-plugin` (not in Ubuntu 22.04 base) and no `socat`/`certbot`/`gcloud`.

```bash
gcloud compute instances create s3lm-budget \
  --project=project-9e3226b0-2632-488c-b87 --account=v2660228@gmail.com \
  --zone=asia-south1-a \
  --machine-type=t2d-standard-8 \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=100GB --boot-disk-type=pd-balanced \
  --address=s3lm-ip \
  --tags=s3lm-web \
  --metadata=startup-script='#!/bin/bash
set -e
apt-get update -qq
apt-get install -y -qq docker.io git socat certbot tmux curl
systemctl enable --now docker
# compose v2 (Ubuntu 22.04 package name)
apt-get install -y -qq docker-compose-v2 || true
# gcloud CLI (needed by backup_state_db.sh, which runs as root)
snap install google-cloud-cli --classic || true
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
  sysctl -w vm.swappiness=10
fi'
```

Then copy the deploy key and clone (as the SSH user `prism`):

```bash
# Key: /home/prism/.ssh/s3lm_github_deploy on the old VM (read-only GitHub deploy key)
# It is already registered on vijaykavuri43-sudo/cricketbetting; copying it is enough.
# Also copy /root/.ssh/github_deploy + /root/.ssh/config and root's gitconfig
# safe.directory entry.

sudo mkdir -p /opt/s3lm && sudo chown "$USER":root /opt/s3lm
git clone git@github.com:vijaykavuri43-sudo/cricketbetting.git /opt/s3lm/s3-live-monitor
```

Apply the uncommitted patch captured in Phase 1, or commit it to `main` first:

```bash
cd /opt/s3lm/s3-live-monitor
git apply --check /tmp/mig/vm-uncommitted.patch && git apply /tmp/mig/vm-uncommitted.patch
```

Build now (warms the Docker layer cache) but **do not start the stack yet**:

```bash
cd /opt/s3lm/s3-live-monitor && sudo docker compose build
```

Create the six volumes with the exact compose-prefixed names:

```bash
for v in s3lm-data s3lm-data-b2 s3lm-data-b3 s3sync-state s3sync-state-b2 s3sync-state-b3; do
  sudo docker volume create "s3-live-monitor_$v"
done
```

The prefix is `s3-live-monitor_` because the compose project directory is
`/opt/s3lm/s3-live-monitor`. Do not rename the directory.

---

## Phase 3 — bulk copy while the old stack is live

> **Strategy 1 (project transfer): skip the GCS subsection below.** The bucket is already
> in the transferred project; the new VM only needs IAM access to it. The volume pre-copy
> in Phase 3.2 still applies if you build a new VM.

### GCS bucket (the long pole) — Strategy 2 only

Run on the **new VM** in `tmux` (it has the metadata service account with viewer on the
source and objectAdmin on the destination). `gcloud storage rsync` is resumable and skips
objects already present with matching size/checksum.

```bash
tmux new -s copy
gcloud storage rsync --recursive \
  gs://cbtf-case-evidence-518405669914 \
  gs://cbtf-case-evidence-114849318697
```

- Expected volume: **~879 GB / ~2.99 M objects** (plus `_backups/`, `manifests/`, `_test/`).
- Object-count bound: 2–8 hours. Do not interrupt for transient 403/429; rerun resumes.
- **Never** add `--delete-unmatched-destination-objects` on the initial pass.
- Monitor: `gcloud storage du -s gs://cbtf-case-evidence-114849318697` (slow) or sample
  prefixes; the final acceptance check is in Phase 6.

If resumability or speed is a concern, use **Storage Transfer Service** instead of
`rsync`; it is managed and retries per object, but requires the same source/dest IAM.

### 5.2 Volume pre-copy (warm the transfer)

Start this after the GCS copy has a stable rate, or in parallel if bandwidth allows.
On the **old VM** (as a `prism` user with passwordless sudo; staging in the old bucket):

```bash
sudo mkdir -p /tmp/mig
for v in \
  s3-live-monitor_s3lm-data s3-live-monitor_s3lm-data-b2 s3-live-monitor_s3lm-data-b3 \
  s3-live-monitor_s3sync-state s3-live-monitor_s3sync-state-b2 s3-live-monitor_s3sync-state-b3; do
  sudo tar -C "/var/lib/docker/volumes/$v/_data" -czf "/tmp/mig/$v.tgz" .
done
sudo tar -C /opt/s3lm -czf /tmp/mig/intelligence.tgz intelligence
sudo tar -C /opt/s3lm/s3-live-monitor -czf /tmp/mig/app-env.tgz .env
sha256sum /tmp/mig/*.tgz | sudo tee /tmp/mig/SHA256SUMS
gcloud storage cp /tmp/mig/*.tgz /tmp/mig/SHA256SUMS \
  gs://cbtf-case-evidence-518405669914/_migration/2026-10-24/
```

This pass is **not** consistent (the stack is live) — it only warms the cache. It is
replaced in Phase 4 after the containers are stopped.

On the new VM, download and unpack the warm copy into the volumes:

```bash
gcloud storage cp 'gs://cbtf-case-evidence-518405669914/_migration/2026-10-24/*' /tmp/mig/
cd /tmp/mig && sha256sum -c SHA256SUMS
for v in ...; do sudo tar -C "/var/lib/docker/volumes/$v/_data" -xzf "/tmp/mig/$v.tgz"; done
sudo tar -C /opt/s3lm -xzf /tmp/mig/intelligence.tgz
sudo tar -C /opt/s3lm/s3-live-monitor -xzf /tmp/mig/app-env.tgz
```

---

## Phase 4 — freeze, final delta, cutover

**Downtime starts here. Target: 30–60 minutes.**

1. **Freeze the producers, then the apps** (order matters):
   ```bash
   sudo docker stop s3sync s3sync-b2 s3sync-b3
   sudo docker stop s3lm s3lm-b2 s3lm-b3
   ```
   Confirm no writer processes remain: `sudo docker ps` shows all six stopped/exited.

2. **Final GCS delta** (from the new VM, same rsync command):
   ```bash
   gcloud storage rsync --recursive \
     gs://cbtf-case-evidence-518405669914 \
     gs://cbtf-case-evidence-114849318697
   ```
   Only objects written since the bulk pass are copied. Do **not** delete source or use
   `--delete-*`.

3. **Final volume copy** (old VM, containers stopped):
   ```bash
   rm -f /tmp/mig/*.tgz /tmp/mig/SHA256SUMS
   for v in ...; do sudo tar -C "/var/lib/docker/volumes/$v/_data" -czf "/tmp/mig/$v.tgz" .; done
   sudo tar -C /opt/s3lm -czf /tmp/mig/intelligence.tgz intelligence
   sudo tar -C /opt/s3lm/s3-live-monitor -czf /tmp/mig/app-env.tgz .env
   sha256sum /tmp/mig/*.tgz | sudo tee /tmp/mig/SHA256SUMS
   gcloud storage cp /tmp/mig/*.tgz /tmp/mig/SHA256SUMS \
     gs://cbtf-case-evidence-518405669914/_migration/2026-10-24-final/
   ```
   Download on the new VM, `sha256sum -c`, then extract with `--numeric-owner`:
   ```bash
   for v in ...; do sudo tar --numeric-owner -C "/var/lib/docker/volumes/$v/_data" -xzf "/tmp/mig/$v.tgz"; done
   sudo tar --numeric-owner -C /opt/s3lm -xzf /tmp/mig/intelligence.tgz
   sudo tar --numeric-owner -C /opt/s3lm/s3-live-monitor -xzf /tmp/mig/app-env.tgz
   ```

4. **DB integrity before starting the stack**:
   ```bash
   # Run each DB through PRAGMA integrity_check using a throwaway python container
   for v in s3lm-data s3lm-data-b2 s3lm-data-b3; do
     sudo docker run --rm -v "s3-live-monitor_$v:/d" python:3.12-slim \
       python -c "import sqlite3;c=sqlite3.connect('/d/manifest.db');print('$v',c.execute('pragma integrity_check').fetchone()[0])"
   done
   for v in s3sync-state s3sync-state-b2 s3sync-state-b3; do
     sudo docker run --rm -v "s3-live-monitor_$v:/d" python:3.12-slim \
       python -c "import sqlite3;c=sqlite3.connect('/d/state.db');print('$v',c.execute('pragma integrity_check').fetchone()[0])"
   done
   ```

5. **Point the app at the new bucket / IP** — edit `/opt/s3lm/s3-live-monitor/.env`:
   ```dotenv
   # Strategy 2 (bucket copied to the new project):
   S3LM_GCS_BUCKET=cbtf-case-evidence-114849318697
   S3SYNC_GCS_BUCKET=cbtf-case-evidence-114849318697
   # Strategy 1 (project transferred): keep the existing bucket name
   # S3LM_GCS_BUCKET=cbtf-case-evidence-518405669914

   S3LM_PUBLIC_URL=http://<NEW_IP>
   S3LM_PUBLIC_URL_B2=http://<NEW_IP>:81
   ```
   Keep the existing `S3LM_GEMINI_API_KEY` / `S3LM_GROQ_API_KEY` lines.

6. **Recreate host config** (copy from the old VM via staging):
   - `/usr/local/bin/s3lm-watchdog.sh` + `/etc/cron.d/s3lm-watchdog`
   - `/usr/local/bin/backup_state_db.sh` + `/etc/cron.d/s3lm-db-backup`
     → **change the default bucket** in the script (or add `S3LM_GCS_BUCKET=…` to the
     cron file) or the 03:00 backup will target the old bucket.
   - `/etc/systemd/system/s3lm-tls.service`
   - root's `/root/.ssh/github_deploy`, `/root/.ssh/config`, and
     `git config --global --add safe.directory /opt/s3lm/s3-live-monitor`

7. **Issue a new TLS cert** for the new IP (the old cert is for
   `8-231-108-145.nip.io`). Port 80 must be free; do this before starting `s3lm`:
   ```bash
   DASHED=$(echo "$NEW_IP" | tr . -)
   sudo certbot certonly --standalone --non-interactive --agree-tos \
     -m <ops-email> -d "${DASHED}.nip.io"
   sudo sh -c "cat /etc/letsencrypt/live/${DASHED}.nip.io/fullchain.pem \
     /etc/letsencrypt/live/${DASHED}.nip.io/privkey.pem > /etc/s3lm-le.pem"
   sudo chmod 600 /etc/s3lm-le.pem
   sudo systemctl daemon-reload && sudo systemctl enable --now s3lm-tls
   ```
   Add a renewal deploy hook that re-concatenates the PEM, otherwise renewal breaks
   `socat` after 90 days.

8. **Start and verify**:
   ```bash
   cd /opt/s3lm/s3-live-monitor && sudo docker compose up -d
   sleep 10
   sudo docker ps --format 'table {{.Names}}\t{{.Status}}'
   for p in 80 81 82; do curl -fsS "http://127.0.0.1:$p/api/healthz"; echo; done
   ```

9. **Switch access**: publish `http://<NEW_IP>/`, `:81`, `:82`, `https://<DASHED>.nip.io:8443`.
   If a DNS name is added later, update `S3LM_PUBLIC_URL` and redeploy.

---

## Phase 5 — acceptance checks (no sign-off without these)

> **Strategy 1 (project transfer):** the three GCS copy/hash rows are **N/A** — the bucket
> never moved. Instead verify `v2660228` is the only project Owner, can list/read/write the
> bucket, and that `vijaykavuri72` has no remaining roles.

| Check | Expected |
|---|---|
| GCS total bytes (old vs new) | Within the delta created during the freeze window; both `gcloud storage du -s` |
| GCS object count old vs new | Equal (± objects written after the final rsync; none if freeze held) |
| Prefix counts | `s3-mirror/`, `s3-mirror-bxs3dev001/`, `s3-mirror-tw-artifacts/`, `*-sync/`, `_backups/`, `manifests/` all match |
| Hash spot-check | 20 random objects: `gcloud storage hash gs://old/key gs://new/key` — MD5/CRC32C identical |
| b1 manifest | `count(*), sum(size)` = baseline (may grow with post-freeze writes; compare against a fresh pre-freeze baseline) |
| b2 manifest | 731,278 / 176,965,750,701 (baseline) |
| b3 manifest | 65,906 / 7,818,660,601 (baseline) |
| Sync state | `synced` rows ≥ manifest; `pending_failures`/`terminal_failures` preserved (b3 = 31 terminal) |
| SQLite | `PRAGMA integrity_check` = `ok` on all six DBs |
| Dashboards | `:80/:81/:82 /api/healthz` = 200; KPIs (total objects, 24h mix, rails) match old |
| Downloads | A slip image + a wallet/QR object download from each dashboard |
| Auth | Existing shared token still works (it lives in the copied volumes) |
| Sync | `s3sync*` logs show cycles completing, uploads resumed, errors=0; new objects appear in the **new** bucket |
| Audit | `collection-log.jsonl` line count ≥ old; new entries reference the new bucket |
| Backup | `backup_state_db.sh` targets the new bucket; run it once manually and verify the object |
| TLS | `curl -k https://<DASHED>.nip.io:8443/api/healthz` = 200; cert CN/SAN is the new name |
| Watchdog | `systemctl status s3lm-tls` active; cron present; no restart loop in syslog |

---

## 8. Rollback

The old system is never modified until decommission, so rollback is immediate:

1. Old VM: `sudo docker start s3lm s3lm-b2 s3lm-b3 s3sync s3sync-b2 s3sync-b3`
   (start sync workers last).
2. Point users back to `http://8.231.108.145/` (or the old DNS record).
3. If the VM itself was damaged, create a disk from snapshot
   `s3lm-pre-migration-20261024` and attach/boot it.
4. Old bucket was never written to after freeze — no reverse copy is needed.

---

## 9. Decommission and retention

- Keep the old VM + old bucket **untouched and running for 14–30 days** after sign-off.
  Old CSV exhibits contain `http://8.231.108.145` links; keeping the old stack alive
  preserves them. Decide a retention window with the case owner.
- After sign-off:
  1. Revoke temporary IAM: new SA `objectViewer` on the old bucket; user grants on the
     old billing account. *(Strategy 2 only.)*
  2. Delete `gs://old/_migration/`. *(Strategy 2 only.)*
  3. Stop the old VM; keep the pre-migration snapshot (cheap).
  4. Unlink the old project from the new billing account, then delete the old project
     **only after** the new bucket copy and the snapshot are independently verified.
     *(Strategy 2 only — under Strategy 1 the old project **is** the production project;
     do not delete it. Just remove the old account's IAM bindings.)*
- Update `INFRA.md` and the deploy-script defaults (`GCP_PROJECT`, bucket name, public
  URL) to the new project/IP so future deploys do not target the old account.

---

## 10. Timeline and cost (estimates)

| Phase | Duration | Notes |
|---|---|---|
| Phase 0 (billing) | 15 min – 1 day | depends on Console/payment action |
| Phase 1 (snapshot) | 10–20 min | 40 GB used |
| Phase 2 (provision + build) | 1–2 h | quota/API waits |
| Phase 3 (bulk GCS) | 2–8 h | ~3 M objects, runs live |
| Phase 3 (volume warm copy) | 30–60 min | ~13 GB, same region |
| Phase 4 (freeze/cutover) | 30–60 min | **downtime** |
| Phase 5 (verification) | 1–2 h | |

> **Strategy 1** drops the bulk GCS row and the bucket overlap cost: total ~3–5 h plus
> the billing action, with the same 30–60 min downtime. **Strategy 2** is the full table.

Costs (approx):
- GCS copy operations: ~3 M Class A ops ≈ **$15–30 one-time**; no egress for
  same-region GCS→GCS. *(Strategy 2 only.)*
- New bucket storage: ~880 GB × ~$0.023/GB ≈ **~$20/mo**; overlap with old bucket
  doubles storage for the transition. *(Strategy 2 only; Strategy 1 keeps one bucket.)*
- New VM `t2d-standard-8`: roughly **$250–330/mo** on-demand in `asia-south1`
  (verify current pricing); old + new overlap doubles VM cost until decommission.
- Snapshot: ~40 GB ≈ **$1–2/mo**.
- Re-syncing from AWS S3 (fallback only) would add ~$80+ egress and would **not** recover
  objects already deleted upstream — avoid.

---

## 11. Risks and gotchas

1. **Billing closure may escalate** to resource suspension/deletion. Phase 0 is the only
   urgent item; do not delete the project to “fix” billing.
2. **Uncommitted VM code**: 63 modified files exist only on the old disk. Export the diff
   in Phase 1; a clean `git clone` will not contain them.
3. **Cross-account billing link** requires a billing admin grant; confirm currency (both
   INR) and payment method.
4. **T2D quota** in the new project may be 0. Check before creating; fall back to
   `e2-standard-8`.
5. **Compute API** is not enabled in the new project (confirmed).
6. **GCS copy is object-count bound**, not byte bound. Run it in `tmux`, make it
   resumable, and never delete the source until sign-off.
7. **SQLite consistency**: the warm volume copy is not consistent; only the post-freeze
   copy is. Stop `s3sync*` first, then apps, and verify `integrity_check`.
8. **Volume names** must remain `s3-live-monitor_*`; keep the app directory name.
9. **TLS cert is IP-bound** to `8-231-108-145.nip.io`; the new VM needs a new cert for
   the new static IP and a renewal hook.
10. **Hardcoded old IP** in `docker-compose.yml` defaults and `S3LM_PUBLIC_URL`; `.env`
    overrides on the VM, but update repo defaults after cutover.
11. **Nightly DB backup defaults to the old bucket**; update the script/cron or it will
    silently write to the wrong place (or fail).
12. **Old IP is already static** (`s3lm-budget-ip` = `8.231.108.145`), not ephemeral —
    old exhibit links stay valid as long as the address is not released. External IPs
    cannot be moved between projects, so the new VM still needs its own reserved static
    IP.
13. **Do not copy stale volumes** (`s3lm-data`, `s3lm_s3lm-data`) — they are from an old
    compose project.
14. **Secrets**: `.env` holds Gemini/Groq keys; stage it in GCS (encrypted at rest) and
    delete `_migration/` after cutover.
15. **Status prefixes** (`*-sync/`) are part of the dashboard; copy them or the new
    dashboards will show “starting” until the first sync cycle rewrites them.
16. **Sync backlog**: uploads paused at `2026-10-23T23:23:13Z`. After billing is restored
    the workers catch up from S3, but any object deleted upstream during the gap may be
    unrecoverable. Restore access first, then let the sync drain before freezing.
