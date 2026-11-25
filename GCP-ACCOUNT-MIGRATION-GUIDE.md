# GCP Account / Project Migration Guide

## Current deployment — 2026-11-25

The completed current transfer is **v2660228@gmail.com → jk5391775@gmail.com**.
The existing project is now **S3 Live Monitor**, ID
`project-9641a7b9-3759-4c98-9b8`, under **jk5391775-org** (`580417351522`), with
billing account **`01D42C-5BB8A5-0CD6DA`**. The VM, disks, static IP, bucket,
service account and volumes were preserved in place. The new operator has
Editor, Project Billing Manager and Project IAM Admin access. Old-account
project, VM and bucket reads were verified denied after its project bindings
were removed. Temporary migration policies and organization grants were removed.

For current operations use [`INFRA.md`](INFRA.md) and
`./deploy/deploy.sh --check`; deployment scripts select the new account and
check the organization and billing link. In Console choose
**jk5391775-org → S3 Live Monitor**. An external Owner binding required an
invitation during the billing-only phase; the roles above provide the verified
management access used for this migration.

**The remaining sections record October's migration and reusable examples.**
October account IDs, organization IDs, billing IDs and account-removal commands
are historical, not the current deployment defaults. Do not rerun those cleanup
or account-closure commands on the November accounts. Other old-account projects
remain outside this monitoring-project transfer.

## Historical runbook — October 2026

> **Field-tested runbook** based on the 2026-10-25 migration of the
> `s3-live-monitor` stack from `vijaykavuri72@gmail.com` to
> `v2660228@gmail.com`.
>
> **Golden rule:** move the **project**, not the VM or bucket separately.
> A project move is metadata-only, free, and preserves the project ID/number,
> VM, disks, static IP, GCS bucket, IAM, service accounts, API keys, enabled
> APIs, snapshots, and application links.
>
> Related files: [`MIGRATION-PLAN.md`](MIGRATION-PLAN.md),
> [`INFRA.md`](INFRA.md).

---

## 0. TL;DR

If the VM and bucket are in the same project and you are moving to a new
Google account that owns an organization:

1. Inventory and back up metadata + VM snapshot.
2. Grant the new account the required source-project and destination-org roles.
3. Set temporary cross-org policy allowlists using
   `under:organizations/ORG_ID`.
4. Move the project with the Resource Manager v3 `projects.move` API.
5. Relink the project to the new billing account.
6. Remove the old account from project/bucket/SSH access.
7. Verify the new account sees the VM and bucket; verify the old account sees
   nothing.
8. Delete the empty old orgs in the Console, then close the old account after
   reviewing its billing debt.

**Do not copy an 879 GB bucket or rebuild a VM unless the project move is
impossible.** Copying costs time and money; rebuilding risks losing Docker
volumes, configuration, and the static IP.

---

## 1. Scope

### Covered

- Moving a GCP project from one Google account/organization to another.
- Moving a VM, boot disk, static IP, GCS bucket, service accounts, API keys,
  enabled APIs, IAM, snapshots, and application links with the project.
- Cross-organization policy allowlists.
- Relinking billing after the move.
- Removing old-account access and verifying it is gone.
- Deleting empty old organizations and closing the old account.
- A real case study with exact IDs and failure modes.

### Not covered

- AWS S3 source buckets. Those are AWS resources; a Google account move does
  not touch them.
- Google Workspace / Cloud Identity domain transfers.
- Transferring a billing account itself. Billing accounts are financial
  accounts; create/use a new one and relink projects. Old debt stays with the
  old billing account.
- Google Drive, Photos, Gmail, or other consumer Google data.
- External DNS, GitHub, payment cards, or support plans.
- Cross-cloud migrations.

---

## 2. Decision matrix: move project vs copy bucket vs rebuild VM

| Situation | Best approach | Why |
|---|---|---|
| VM and bucket are in the same project; old account is being closed | **Move the project** | Zero-copy; preserves IDs, IP, bucket, IAM, service accounts, API keys, links |
| Only a bucket needs to move; project stays | Create a new bucket and copy with `gcloud storage rsync` or Storage Transfer Service | Copy costs egress/operations; bucket name must be globally unique; update references |
| Only a VM needs to move; project stays | Machine image/snapshot + new VM in destination project | Recreating can change the external IP; Docker volumes/config must be reattached |
| Multiple projects | Move each project; repeat the same steps | Project move is per-project |
| Project is standalone (no organization) | Move it into the destination organization | Destination import policy may or may not apply; test with a non-critical project first |
| Need the same static IP and existing links | **Move the project** | The regional static IP stays with the project |
| Old account is already locked | You cannot move the project | Restore access/pay debt, or rebuild from snapshots/backups |
| Legal/evidence chain of custody | Move the project and record hashes/timestamps | No object copy means fewer integrity risks |

### Why not copy the bucket?

- 879 GB of egress + operations can cost real money and take hours/days.
- Object metadata, ACLs, retention, and generation numbers may not copy
  perfectly.
- You then have two buckets to secure and reconcile.
- The project move keeps the same bucket name and objects in place.

### Why not rebuild the VM?

- Docker volumes, `/opt/s3lm/intelligence`, `.env`, TLS, watchdog, cron, and
  local state must be recreated.
- The static IP and existing evidence links can change.
- A project move keeps the VM running with the same disk and IP.

---

## 3. What moves with a project

A project move changes the project's **parent** (organization/folder). It does
not copy data. These resources normally stay with the project:

- Compute Engine VM instances, disks, snapshots, images, instance templates
- Regional/global static IP addresses
- VPC networks, subnets, firewall rules, routes
- GCS buckets and objects (the bucket is in the project, not inside the VM)
- BigQuery datasets/tables
- Cloud SQL, Cloud Run, Cloud Functions, GKE, Artifact Registry
- Service accounts and their IAM policies
- API keys
- Enabled APIs
- Project IAM policy
- Cloud DNS zones in the project
- Secrets, Pub/Sub topics, Scheduler jobs
- Logging/monitoring resources
- Project-level quotas and labels

### What does not move automatically

- **Billing link:** the project may still be linked to the old billing account
  after the move. Relink it to the new billing account.
- **Inherited IAM from the old organization/folder:** users who relied only on
  old-org roles lose access. Grant project-level roles to the new account.
- **Organization policies:** the new org's policies apply. Check for stricter
  constraints (domain-restricted sharing, service account key creation, etc.).
- **Shared VPC / VPC Service Controls / KMS dependencies:** these may live in
  another project or org. Plan separately.
- **Committed use discounts, billing budgets, support plan:** billing-specific;
  review and recreate.
- **External DNS, GitHub, AWS, payment methods:** separate systems.

### Important

- Project **ID** and **number** are immutable. A project ID that looks like it
  belongs to the old account is just a name; ownership is the org parent.
- Service account emails contain the project number and stay the same.
- Bucket names are globally unique and stay the same.

---

## 4. Pre-flight checklist

### 4.1 Record the IDs first

Fill this in before changing anything:

| Field | Value |
|---|---|
| Migration date/window | |
| Old Google account | |
| New Google account | |
| Source organization ID/name | |
| Destination organization ID/name | |
| Project ID | |
| Project number | |
| Project display name | |
| VM name / zone / machine type | |
| VM external IP / static IP name | |
| Bucket name / location / size | |
| Old billing account / status | |
| New billing account / status | |
| Snapshot name | |
| Critical backup path | |
| Old org deletion date | |
| Old account closure date | |

### 4.2 Inventory commands

Run these as the **old account** (or as the new account after access is
granted). Save the output.

```bash
OLD_ACCOUNT="old@example.com"
NEW_ACCOUNT="new@example.com"
PROJECT_ID="your-project-id"
BUCKET="your-bucket-name"
VM="your-vm-name"
ZONE="asia-south1-a"
SOURCE_ORG="111111111111"
DEST_ORG="222222222222"

gcloud auth list
gcloud organizations list --account="$OLD_ACCOUNT"
gcloud projects list --account="$OLD_ACCOUNT" \
  --format="table(projectId,projectNumber,parent.type,parent.id,lifecycleState)"
gcloud billing accounts list --account="$OLD_ACCOUNT"

gcloud projects describe "$PROJECT_ID" --account="$OLD_ACCOUNT" --format=json
gcloud projects get-ancestors "$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud projects get-iam-policy "$PROJECT_ID" --account="$OLD_ACCOUNT" --format=json

gcloud services list --enabled --project="$PROJECT_ID" --account="$OLD_ACCOUNT"

gcloud compute instances list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud compute disks list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud compute addresses list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud compute snapshots list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"

gcloud storage buckets list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud storage buckets describe "gs://$BUCKET" --account="$OLD_ACCOUNT" --format=json
gcloud storage buckets get-iam-policy "gs://$BUCKET" --account="$OLD_ACCOUNT" --format=json

gcloud iam service-accounts list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud services api-keys list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"

# Optional service inventory
gcloud sql instances list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud run services list --project="$PROJECT_ID" --account="$OLD_ACCOUNT" --platform=managed
gcloud functions list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud container clusters list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud dns managed-zones list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud secrets list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud pubsub topics list --project="$PROJECT_ID" --account="$OLD_ACCOUNT"

# BigQuery: gcloud has no `gcloud bigquery`; use bq or REST
bq ls --project_id="$PROJECT_ID" 2>/dev/null || true

# SSH/OS Login metadata
gcloud compute project-info describe --project="$PROJECT_ID" --account="$OLD_ACCOUNT" --format=json
gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT_ID" --account="$OLD_ACCOUNT" --format=json
```

> **WSL note:** if `gcloud` is installed on Windows and you are in WSL, prefix
> commands with `cmd.exe /c "gcloud ..."`. Keep `--account` and `--project`
> explicit in scripts to avoid operating on the wrong account.

### 4.3 Backup before moving

At minimum:

1. **VM snapshot** (or disk snapshot).
2. **Critical config archive** of `/opt/s3lm/intelligence`, `.env`, compose
   files, TLS, watchdog, cron.
3. **Metadata backup**: project/org/billing JSON, IAM, enabled services,
   service accounts, API keys, bucket IAM, SSH keys.
4. **GCS backup objects** of the critical archive; keep `SHA256SUMS`.
5. **A known-good object read test** (not a full 879 GB copy).

Example snapshot:

```bash
gcloud compute disks snapshot DISK_NAME \
  --zone="$ZONE" \
  --snapshot-names="pre-migration-$(date +%Y%m%d)" \
  --project="$PROJECT_ID" \
  --account="$OLD_ACCOUNT"
```

Metadata backup example:

```bash
mkdir -p migration-backup/old-account-metadata
cd migration-backup/old-account-metadata

gcloud organizations list --account="$OLD_ACCOUNT" --format=json > organizations.json
gcloud projects list --account="$OLD_ACCOUNT" --format=json > projects.json
gcloud billing accounts list --account="$OLD_ACCOUNT" --format=json > billing-accounts.json

for p in PROJECT_ID_1 PROJECT_ID_2; do
  gcloud projects describe "$p" --account="$OLD_ACCOUNT" --format=json > "$p-describe.json"
  gcloud projects get-iam-policy "$p" --account="$OLD_ACCOUNT" --format=json > "$p-iam.json"
  gcloud services list --enabled --project="$p" --account="$OLD_ACCOUNT" --format=json > "$p-services.json"
  gcloud iam service-accounts list --project="$p" --account="$OLD_ACCOUNT" --format=json > "$p-service-accounts.json" 2>/dev/null || true
  gcloud services api-keys list --project="$p" --account="$OLD_ACCOUNT" --format=json > "$p-api-keys.json" 2>/dev/null || true
  gcloud storage buckets list --project="$p" --account="$OLD_ACCOUNT" --format=json > "$p-buckets.json" 2>/dev/null || true
  gcloud billing projects describe "$p" --account="$OLD_ACCOUNT" --format=json > "$p-billing.json" 2>/dev/null || true
done
```

For a huge evidence bucket, do **not** run a full recursive object count just
to verify the move. Use a known object read test, bucket metadata, bucket IAM,
and the VM's own health/sync checks.

### 4.4 Billing checks

```bash
gcloud billing accounts list --account="$OLD_ACCOUNT"
gcloud billing accounts list --account="$NEW_ACCOUNT"
gcloud billing accounts describe "$OLD_BILLING" --account="$OLD_ACCOUNT"
gcloud billing accounts describe "$NEW_BILLING" --account="$NEW_ACCOUNT"
gcloud billing projects describe "$PROJECT_ID" --account="$OLD_ACCOUNT"
gcloud billing projects describe "$PROJECT_ID" --account="$NEW_ACCOUNT"
gcloud billing projects list --billing-account="$OLD_BILLING" --account="$OLD_ACCOUNT"
gcloud billing projects list --billing-account="$NEW_BILLING" --account="$NEW_ACCOUNT"
```

Check:

- Old billing account is open or closed; old debt is separate.
- New billing account is **open**.
- New billing account has a valid payment method, independent of the old
  account.
- New billing account has enough **project-link quota** for all active projects.
  If not, request an increase **before** moving active projects:
  <https://support.google.com/code/contact/billing_quota_increase>
- A project can move even if the old billing account is closed, but billable
  services may be limited until it is relinked to the new billing account.

### 4.5 Permission checks

The new account needs, at minimum:

| Resource | Role | Why |
|---|---|---|
| Source project | `roles/resourcemanager.projectIamAdmin` | Required to set project IAM during move; without it, move can return 403 with empty details |
| Source project | `roles/editor` | Day-to-day access after move (optional but useful) |
| Source project | `roles/billing.projectManager` | Link the project to the new billing account |
| Source project (standalone) | `roles/resourcemanager.projectMover` | Move a no-org project |
| Source org/folder (org-owned project) | `roles/resourcemanager.projectMover` | Move a project whose parent is the old org/folder |
| Source org | `roles/orgpolicy.policyAdmin` (temporary) | Set the export allowlist |
| Destination org/folder | `roles/resourcemanager.projectCreator` | Documented requirement to import into the destination |
| Destination org | `roles/resourcemanager.projectMover` | Present in this migration; grant if available |
| Destination org | `roles/orgpolicy.policyAdmin` | Set the import allowlist |
| New billing account | `roles/billing.admin` | Link projects and manage billing |
| Destination org | `roles/resourcemanager.organizationAdmin` | Manage org-level IAM and cleanup |

The old account needs to be able to grant those roles. On a standalone org it
is normally the **Organization Owner**; on a project it is normally
`roles/owner`.

> **Owner-invitation trap:** granting `roles/owner` to an external user can fail
> with `ORG_MUST_INVITE_EXTERNAL_OWNERS` or
> `This owner cannot be accepted to the organisation`. Do not depend on
> project-level Owner. Use `roles/resourcemanager.projectIamAdmin` +
> `roles/editor` + destination-org `Organization Admin`.

### 4.6 Organization-policy checks

Cross-organization moves are blocked by default. Check both orgs:

```bash
gcloud resource-manager org-policies describe \
  constraints/resourcemanager.allowedExportDestinations \
  --organization="$SOURCE_ORG" --effective --format=json \
  --account="$NEW_ACCOUNT"

gcloud resource-manager org-policies describe \
  constraints/resourcemanager.allowedImportSources \
  --organization="$DEST_ORG" --effective --format=json \
  --account="$NEW_ACCOUNT"
```

A default result looks like:

```json
{
  "constraint": "constraints/resourcemanager.allowedImportSources",
  "listPolicy": {
    "allValues": "DENY"
  }
}
```

You will add temporary allowlists later. The allowed value format is critical:

- Correct: `under:organizations/237163885821`
- Wrong: `237163885821`
- Wrong: `organizations/237163885821`

### 4.7 Access/security checks before moving

```bash
# Bucket IAM: old account must not be a member
gcloud storage buckets get-iam-policy "gs://$BUCKET" --format=json \
  | grep -i "$OLD_ACCOUNT" || echo "old account not in bucket IAM"

# Uniform bucket-level access: if true, object ACLs are disabled
gcloud storage buckets describe "gs://$BUCKET" --format=json

# VM service-account IAM
gcloud iam service-accounts get-iam-policy \
  "$(gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT_ID" \
      --format='value(serviceAccounts[0].email)')" \
  --project="$PROJECT_ID" --format=json

# Project and instance SSH keys; remove old users if present
gcloud compute project-info describe --project="$PROJECT_ID" --format=json
gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT_ID" --format=json
```

Check for old-account SSH keys, old-account bucket IAM bindings, HMAC keys, and
service-account impersonation. Project IAM removal alone does not remove
bucket-level IAM or SSH metadata keys.

---

## 5. Step-by-step migration

### Step 1 — Set the working account

Use `--account` explicitly in every command. Confirm:

```bash
gcloud auth list
gcloud config set account "$NEW_ACCOUNT"
gcloud config set project "$PROJECT_ID"
```

For the old-account steps, either switch accounts or pass
`--account="$OLD_ACCOUNT"`.

### Step 2 — Inventory and back up

Run the commands in section 4.2–4.3. Do not skip the snapshot and metadata
backup.

### Step 3 — Grant the new account source-project roles

As the old account:

```bash
# Required for setIamPolicy during move
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectIamAdmin" \
  --account="$OLD_ACCOUNT"

# Day-to-day access after move
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/editor" \
  --account="$OLD_ACCOUNT"

# Needed to relink billing
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/billing.projectManager" \
  --account="$OLD_ACCOUNT"
```

If the project is **standalone** (no org):

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectMover" \
  --account="$OLD_ACCOUNT"
```

If the project is **in an org/folder**, grant Project Mover on the project's
parent:

```bash
# If directly under the org:
gcloud organizations add-iam-policy-binding "$SOURCE_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectMover" \
  --account="$OLD_ACCOUNT"

# If under a folder, grant on the folder instead:
# gcloud resource-manager folders add-iam-policy-binding FOLDER_ID \
#   --member="user:$NEW_ACCOUNT" \
#   --role="roles/resourcemanager.projectMover" \
#   --account="$OLD_ACCOUNT"
```

Temporary org-policy admin on the source org:

```bash
gcloud organizations add-iam-policy-binding "$SOURCE_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/orgpolicy.policyAdmin" \
  --account="$OLD_ACCOUNT"
```

Verify effective permissions:

```bash
TOKEN=$(gcloud auth print-access-token --account="$NEW_ACCOUNT")

curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"permissions":["resourcemanager.projects.move","resourcemanager.projects.setIamPolicy","resourcemanager.projects.get"]}' \
  "https://cloudresourcemanager.googleapis.com/v1/projects/$PROJECT_ID:testIamPermissions"
```

Expected: `resourcemanager.projects.move` and
`resourcemanager.projects.setIamPolicy` must be present. If `setIamPolicy` is
missing, the move can return **403 The caller does not have permission** with
empty `PreconditionFailure` details.

### Step 4 — Confirm destination-org roles

As the new account:

```bash
gcloud organizations add-iam-policy-binding "$DEST_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectCreator" \
  --account="$NEW_ACCOUNT"

gcloud organizations add-iam-policy-binding "$DEST_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectMover" \
  --account="$NEW_ACCOUNT"

gcloud organizations add-iam-policy-binding "$DEST_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/orgpolicy.policyAdmin" \
  --account="$NEW_ACCOUNT"
```

Verify the destination org permission:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"permissions":["resourcemanager.projects.move"]}' \
  "https://cloudresourcemanager.googleapis.com/v3/organizations/$DEST_ORG:testIamPermissions"
```

### Step 5 — Add temporary cross-org allowlists

Only needed for an **org-to-org** move. In this migration, the two standalone
projects moved without allowlists; the org-owned project required both.

Source org — allow export to destination:

```bash
gcloud resource-manager org-policies allow \
  constraints/resourcemanager.allowedExportDestinations \
  "under:organizations/$DEST_ORG" \
  --organization="$SOURCE_ORG" \
  --account="$NEW_ACCOUNT"
```

Destination org — allow import from source:

```bash
gcloud resource-manager org-policies allow \
  constraints/resourcemanager.allowedImportSources \
  "under:organizations/$SOURCE_ORG" \
  --organization="$DEST_ORG" \
  --account="$NEW_ACCOUNT"
```

Verify:

```bash
gcloud resource-manager org-policies describe \
  constraints/resourcemanager.allowedExportDestinations \
  --organization="$SOURCE_ORG" --effective --format=json \
  --account="$NEW_ACCOUNT"

gcloud resource-manager org-policies describe \
  constraints/resourcemanager.allowedImportSources \
  --organization="$DEST_ORG" --effective --format=json \
  --account="$NEW_ACCOUNT"
```

**Wait about 60 seconds** for policy propagation. In this migration, an
immediate retry still failed with the old cached policy; a retry after waiting
succeeded.

### Step 6 — Move the project

#### Option A — REST API (recommended; works when `gcloud projects move` is not installed)

```bash
PROJECT_ID="your-project-id"
DEST_ORG="237163885821"
NEW_ACCOUNT="new@example.com"

TOKEN=$(gcloud auth print-access-token --account="$NEW_ACCOUNT")

RESP=$(curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"destinationParent\":\"organizations/$DEST_ORG\"}" \
  "https://cloudresourcemanager.googleapis.com/v3/projects/$PROJECT_ID:move")

echo "$RESP"

OP=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("name",""))')
if [ -z "$OP" ]; then
  echo "Move did not return an operation; inspect the error above."
  exit 1
fi

for i in $(seq 1 80); do
  STATUS=$(curl -sS -H "Authorization: Bearer $TOKEN" \
    "https://cloudresourcemanager.googleapis.com/v3/$OP")
  echo "$STATUS" | python3 -m json.tool
  DONE=$(echo "$STATUS" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("done",False))')
  if [ "$DONE" = "True" ]; then break; fi
  sleep 3
done
```

A successful operation response contains:

```json
{
  "name": "projects/518405669914",
  "parent": "organizations/237163885821",
  "projectId": "project-9641a7b9-3759-4c98-9b8",
  "state": "ACTIVE"
}
```

#### Option B — `gcloud beta projects move`

If the beta component is installed:

```bash
gcloud beta projects move "$PROJECT_ID" \
  --organization="$DEST_ORG" \
  --account="$NEW_ACCOUNT"
```

If `gcloud projects move` says `Invalid choice: 'move'`, install the beta
component or use the REST API above.

### Step 7 — Verify the move immediately

```bash
gcloud projects describe "$PROJECT_ID" --account="$NEW_ACCOUNT" \
  --format="value(projectId,projectNumber,parent.type,parent.id,lifecycleState)"

gcloud projects get-ancestors "$PROJECT_ID" --account="$NEW_ACCOUNT"

gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT_ID" \
  --account="$NEW_ACCOUNT" \
  --format="value(name,status,zone.basename(),networkInterfaces[0].accessConfigs[0].natIP)"

gcloud compute addresses describe "$STATIC_IP_NAME" --region="$REGION" \
  --project="$PROJECT_ID" --account="$NEW_ACCOUNT" \
  --format="value(name,address,status)"

gcloud storage buckets list --project="$PROJECT_ID" --account="$NEW_ACCOUNT"

gcloud storage ls "gs://$BUCKET/_migration-backup/SHA256SUMS" \
  --account="$NEW_ACCOUNT"

curl -sS -o /dev/null -w "%{http_code}\n" "http://$VM_IP/api/healthz"
```

Expected:

- Parent is the destination org.
- Project ID/number unchanged.
- VM is RUNNING with the same IP.
- Static IP is IN_USE.
- Bucket exists and a known object is readable.
- Health endpoint returns 200.

Also verify:

```bash
gcloud services list --enabled --project="$PROJECT_ID" --account="$NEW_ACCOUNT"
gcloud iam service-accounts list --project="$PROJECT_ID" --account="$NEW_ACCOUNT"
gcloud services api-keys list --project="$PROJECT_ID" --account="$NEW_ACCOUNT"
```

### Step 8 — Relink billing

```bash
gcloud billing projects link "$PROJECT_ID" \
  --billing-account="$NEW_BILLING" \
  --account="$NEW_ACCOUNT" \
  --quiet

gcloud billing projects describe "$PROJECT_ID" --account="$NEW_ACCOUNT"
```

Expected: `billingAccountName` is the new billing account and
`billingEnabled: true`.

If the project was linked to the old billing account, relinking replaces it.
If you need to unlink first:

```bash
gcloud billing projects unlink "$PROJECT_ID" --account="$NEW_ACCOUNT" --quiet
```

#### If billing quota is exceeded

Symptom:

```text
FAILED_PRECONDITION: Precondition check failed.
Cloud billing quota exceeded:
https://support.google.com/code/contact/billing_quota_increase
```

This is a **project-link quota on the new billing account**, not data loss.
Options:

- For active projects: request a quota increase before closing the old account.
- For empty projects: leave them billing-disabled; they contain no data.
- Do not delete the project just because billing cannot be linked.

### Step 9 — Remove old-account access

As the new account, remove the old account's project Owner:

```bash
gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
  --member="user:$OLD_ACCOUNT" \
  --role="roles/owner" \
  --account="$NEW_ACCOUNT" \
  --quiet
```

If the old account has other project roles, list and remove them:

```bash
gcloud projects get-iam-policy "$PROJECT_ID" --account="$NEW_ACCOUNT" \
  --flatten="bindings[].members" \
  --format="table(bindings.role,bindings.members)"
```

Check **bucket-level IAM** separately:

```bash
gcloud storage buckets get-iam-policy "gs://$BUCKET" --format=json \
  | grep -i "$OLD_ACCOUNT" || echo "old account not in bucket IAM"
```

If present, remove the binding:

```bash
gcloud storage buckets remove-iam-policy-binding "gs://$BUCKET" \
  --member="user:$OLD_ACCOUNT" \
  --role="ROLE_NAME" \
  --account="$NEW_ACCOUNT"
```

Check **SSH keys** in project and instance metadata. Remove old-account keys
while keeping current admin keys. If OS Login is enabled, IAM controls SSH; if
not, metadata `ssh-keys` entries are authorization.

Check **service-account IAM** for old-account impersonation:

```bash
gcloud iam service-accounts get-iam-policy \
  "518405669914-compute@developer.gserviceaccount.com" \
  --project="$PROJECT_ID" --account="$NEW_ACCOUNT" --format=json
```

Check **HMAC keys** and **signed URLs** if the old account could create them.
Rotate signing keys/secrets where needed.

#### Old account still sees the project?

Possible causes:

1. **IAM propagation/cache.** In this migration, recently moved projects stayed
   visible to the old account for several minutes. `s3sync` cleared after a few
   minutes; the others cleared after IAM propagation. Wait and recheck.
2. **Old billing association.** A project still linked to the old billing
   account can remain visible to old billing admins. Unlink/relink billing.
3. **Bucket IAM / SSH keys.** These are separate from project IAM.
4. **A remaining project-level or org-level binding.** Check the project IAM
   policy and `testIamPermissions`.

Final check:

```bash
gcloud projects list --account="$OLD_ACCOUNT"
# Expected: [] (empty)

for p in PROJECT_ID_1 PROJECT_ID_2; do
  gcloud projects describe "$p" --account="$OLD_ACCOUNT" --format="value(projectId)" || true
  # Expected: permission denied
done
```

### Step 10 — Clean up temporary roles and policies

Remove temporary source-org roles:

```bash
gcloud organizations remove-iam-policy-binding "$SOURCE_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectMover" \
  --account="$OLD_ACCOUNT"

gcloud organizations remove-iam-policy-binding "$SOURCE_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/orgpolicy.policyAdmin" \
  --account="$OLD_ACCOUNT"
```

Delete the allowlists to restore default deny:

```bash
gcloud resource-manager org-policies delete \
  constraints/resourcemanager.allowedExportDestinations \
  --organization="$SOURCE_ORG" \
  --account="$NEW_ACCOUNT"

gcloud resource-manager org-policies delete \
  constraints/resourcemanager.allowedImportSources \
  --organization="$DEST_ORG" \
  --account="$NEW_ACCOUNT"
```

Verify:

```bash
gcloud resource-manager org-policies describe \
  constraints/resourcemanager.allowedExportDestinations \
  --organization="$SOURCE_ORG" --effective --format=json \
  --account="$NEW_ACCOUNT"

gcloud resource-manager org-policies describe \
  constraints/resourcemanager.allowedImportSources \
  --organization="$DEST_ORG" --effective --format=json \
  --account="$NEW_ACCOUNT"
```

Remove temporary project-level Project Mover if no longer needed. Keep
`roles/editor`, `roles/resourcemanager.projectIamAdmin`, and
`roles/billing.projectManager` for the new account unless you intentionally
replace them with a different least-privilege design.

### Step 11 — Delete the empty old organizations

This is **Console-only**; Resource Manager has no organization-delete API.

1. Sign in as the old account.
2. Open the organization details page.
3. Remove any pending owner invitation for the new account.
4. Delete the organization; enter the org ID to confirm.
5. Repeat for every empty old org.

Console URLs:

```text
https://console.cloud.google.com/organizations/SOURCE_ORG_ID/details
```

Deletion is a **30-day soft delete** and can be restored within 30 days.
Before deleting, confirm the org has **zero projects and zero folders**:

```bash
gcloud projects list --filter="parent.id=$SOURCE_ORG" --account="$OLD_ACCOUNT"
```

### Step 12 — Close the old account (after debt review)

Before closing the old Google account:

- Delete the empty old orgs.
- Verify the old account's project list is empty.
- Review old billing debt; closing the account does **not** clear it.
- Ensure the new account's billing payment method and recovery email/phone are
  independent of the old account.
- Revoke old gcloud credentials on shared machines:

```bash
gcloud auth revoke "$OLD_ACCOUNT"
```

- Remove the old account from browser profiles/password managers.
- Confirm the new account can access the VM, bucket, API keys, and billing.

### Step 13 — Post-migration security and operations

- Rotate credentials the old account may have seen: API keys, service-account
  keys, SSH keys, `.env` secrets, GitHub/AWS tokens.
- Update monitoring, DNS, bookmarks, runbooks, and CI/CD secrets.
- Recreate billing budgets/alerts on the new billing account.
- Test from an incognito browser as the new account.
- Verify the old account has no bucket IAM, SSH keys, or service-account
  impersonation.
- Keep the VM snapshot and critical archive until the new setup is proven.

---

## 6. Verification checklist

Copy this into the migration record and tick each item:

- [ ] New account active; old account credentials not used for final checks.
- [ ] Project parent is the destination org.
- [ ] Project ID and number unchanged.
- [ ] VM is RUNNING with the same IP.
- [ ] Static IP is IN_USE.
- [ ] App health endpoint returns 200.
- [ ] Bucket exists in the same location.
- [ ] Known object read succeeds as the new account.
- [ ] Bucket write test succeeds (use a temporary object, then delete it).
- [ ] VM service account still has bucket access.
- [ ] Enabled APIs are unchanged.
- [ ] Service accounts and API keys are intact.
- [ ] Billing is linked to the new open account.
- [ ] Old billing account has zero linked projects.
- [ ] Old account project list is `[]`.
- [ ] Old account is denied on every moved project.
- [ ] Old account is not in bucket IAM.
- [ ] No old-account SSH keys in project/instance metadata.
- [ ] Old account is not in service-account IAM.
- [ ] Temporary org-policy allowlists are deleted.
- [ ] Temporary source-org roles are removed.
- [ ] Old orgs are empty and deleted.
- [ ] Old account debt reviewed; account closure decision recorded.
- [ ] Snapshot and metadata backups retained.
- [ ] Docs and monitoring updated.

---

## 7. Rollback plan

A project move can usually be reversed, but **do not rely on rollback**:

1. Keep the old org alive until verification is complete.
2. Keep the VM snapshot and critical archive.
3. To move back, you need the same permissions on the project and both orgs,
   plus reverse org-policy allowlists:
   - Allow export from the new org to the old org.
   - Allow import into the old org from the new org.
4. Move the project back with the same API.
5. Relink billing to an open billing account.
6. Restore any IAM that was removed.

Rollback becomes harder or impossible if:

- the old organization is deleted;
- the old billing account is closed and cannot be reopened;
- the old account is locked;
- bucket IAM or SSH keys were removed;
- the project was deleted.

**The safest rollback is the snapshot/backup, not a reverse move.**

---

## 8. Field notes: errors seen in the real migration

| Symptom | Cause | Fix |
|---|---|---|
| `gcloud projects move` → `Invalid choice: 'move'` | Move command not installed in the base gcloud | Use Resource Manager v3 REST API or install the beta component |
| Move → `403 The caller does not have permission` with empty `PreconditionFailure` | New account lacked `resourcemanager.projects.setIamPolicy` on the source project | Grant `roles/resourcemanager.projectIamAdmin` |
| Move → 403 for an org-owned project | New account lacked `resourcemanager.projects.move` on the source org | Grant `roles/resourcemanager.projectMover` on the source org/folder |
| `Constraint allowedExportDestinations violated` | Source org default-deny policy | Add `under:organizations/DEST_ORG` allowlist |
| `Constraint allowedImportSources violated` | Destination org default-deny policy | Add `under:organizations/SOURCE_ORG` allowlist |
| `Invalid list value` for org policy | Used bare ID or `organizations/ID` | Use `under:organizations/ORG_ID` |
| Move still blocked right after allowlist | Policy propagation delay | Wait ~60 seconds and retry |
| Billing link → `Cloud billing quota exceeded` | New billing account project-link quota | Request increase; leave empty projects disabled |
| Old account still sees project after IAM removal | IAM cache or old billing association | Wait; unlink old billing; recheck |
| `gcloud billing accounts get-iam-policy` → `INVALID_ARGUMENT` | gcloud/API quirk with this billing account | Use REST `GET .../billingAccounts/ID:getIamPolicy` |
| `gcloud bigquery` → invalid choice | BigQuery is not a gcloud command group | Use `bq` or the BigQuery REST API |
| `ORG_MUST_INVITE_EXTERNAL_OWNERS` | Granting Owner to an external user | Use Project IAM Admin + Editor + destination Organization Admin |
| Old account can still SSH to VM | SSH keys in project/instance metadata survive project move | Remove old-account keys; use OS Login/IAM |
| Old account can still read bucket | Bucket-level IAM or object ACLs | Check bucket IAM; use uniform bucket-level access |
| Project still linked to old billing after move | Billing is not part of project move | Relink to new billing account |
| Old account lock due billing debt | Old account's financial issue | Moved resources are safe if new billing/IAM are independent; check shared payment method/recovery email |

---

## 9. Case study: the s3-live-monitor migration

### Before

| Item | Value |
|---|---|
| Old account | `vijaykavuri72@gmail.com` |
| New account | `v2660228@gmail.com` |
| Old orgs | `755663317`, `1052014037805` (both named `vijaykavuri72-org`) |
| New org | `v2660228-org` (`237163885821`) |
| Main project | `project-9641a7b9-3759-4c98-9b8` (#518405669914) |
| VM | `s3lm-budget`, `asia-south1-a`, `t2d-standard-8` |
| VM IP / static IP | `8.231.108.145` / `s3lm-budget-ip` |
| Bucket | `cbtf-case-evidence-518405669914`, `ASIA-SOUTH1` |
| Old billing | `01B553-891570-5D2D90` — CLOSED |
| New billing | `01A4FE-5BE581-D29766` — OPEN |

Other old-account projects:

- `gen-lang-client-0791146751` (#511645898757) — Gemini API key + 3 service accounts.
- `project-9dd84106-d3f1-4f89-b4e` (#532069119571) — empty.
- `s3sync-508116` (#1022063185539) — empty.

The new account also had its own `project-9e3226b0-2632-488c-b87`
(#114849318697), not from the old account.

### What was done

1. Main project moved first (2026-10-25 01:01 UTC).
2. VM snapshot `s3lm-pre-migration-20261024` and critical archives retained.
3. Remaining projects moved:
   - `s3sync-508116` — 15:18 UTC
   - `gen-lang-client-0791146751` — 15:19 UTC
   - `project-9dd84106-d3f1-4f89-b4e` — 15:24 UTC
4. The two standalone projects moved without org-policy allowlists.
5. The org-owned project required both allowlists and temporary source-org
   Project Mover.
6. `gen-lang-client` was linked to the new billing account.
7. The two empty projects were left billing-disabled because the new billing
   account hit its project-link quota.
8. Old account Owner bindings were removed; temporary roles/policies deleted.
9. Both old orgs were left empty and pending Console deletion.

### Final verified state

```text
project-9641a7b9-3759-4c98-9b8  518405669914  organization 237163885821  ACTIVE
s3lm-budget  RUNNING  asia-south1-a  8.231.108.145
s3lm-budget-ip  8.231.108.145  IN_USE
gs://cbtf-case-evidence-518405669914/_migration-backup/SHA256SUMS  readable
http://8.231.108.145/api/healthz  200
old account projects  []
old account access to all four projects  denied
old billing projects  (none)
new billing projects  main + gen-lang-client + new account's own project
```

### Backups retained

- Snapshot `s3lm-pre-migration-20261024` — READY.
- GCS `gs://cbtf-case-evidence-518405669914/_migration-backup/`:
  `app-and-intelligence.tgz`, `decoded-dbs-2026-10-24.tgz`, `SHA256SUMS`.
- Local `migration-backup/small-critical.tgz` — SHA256
  `c3aa4b323a9477d6fe7a2440ad56010a129ea68c36d62a05acf00b5a10f39761`.
- Pre-move metadata in `migration-backup/old-account-metadata/`.

### Cleanup status

API-side cleanup was completed and verified on 2026-10-25:

- Old account has **zero projects**.
- All four moved projects deny access.
- Old account is **not** in bucket IAM, new-org IAM, VM service-account IAM, or
  project/instance SSH-key metadata.
- Old billing account has **zero linked projects**.
- Local gcloud credential for the old account was revoked.

### Pending at the time of writing

- Delete old orgs `755663317` and `1052014037805` in the Console. The old account
  still has Organization Owner roles there because that is required for deletion.
- Review old billing debt before closing the old Google account.
- If the two empty projects need billing, request a quota increase.

---

## 10. Migration worksheet template

```text
Migration name:
Date/window:
Old Google account:
New Google account:
Source org ID/name:
Destination org ID/name:
Project ID:
Project number:
Project display name:
VM name/zone/type:
VM IP / static IP name:
Bucket name/location/size:
Old billing account/status:
New billing account/status:
Snapshot name:
Critical backup path:
Temporary allowlists created? (Y/N)
Temporary roles removed? (Y/N)
Old account project list empty? (Y/N)
Old orgs deleted? (Y/N)
Old billing debt reviewed? (Y/N)
Old account closed? (Y/N)
Notes/errors:
```

---

## 11. One-page command reference

### Inspect

```bash
gcloud auth list
gcloud organizations list
gcloud projects list --format="table(projectId,projectNumber,parent.id,lifecycleState)"
gcloud projects describe "$PROJECT_ID"
gcloud projects get-ancestors "$PROJECT_ID"
gcloud billing accounts list
gcloud billing projects describe "$PROJECT_ID"
gcloud services list --enabled --project="$PROJECT_ID"
gcloud compute instances list --project="$PROJECT_ID"
gcloud compute addresses list --project="$PROJECT_ID"
gcloud storage buckets list --project="$PROJECT_ID"
gcloud storage buckets get-iam-policy "gs://$BUCKET"
gcloud iam service-accounts list --project="$PROJECT_ID"
gcloud services api-keys list --project="$PROJECT_ID"
```

### Roles

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectIamAdmin" \
  --account="$OLD_ACCOUNT"

gcloud organizations add-iam-policy-binding "$SOURCE_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectMover" \
  --account="$OLD_ACCOUNT"

gcloud organizations add-iam-policy-binding "$DEST_ORG" \
  --member="user:$NEW_ACCOUNT" \
  --role="roles/resourcemanager.projectCreator" \
  --account="$NEW_ACCOUNT"
```

### Org policies

```bash
gcloud resource-manager org-policies allow \
  constraints/resourcemanager.allowedExportDestinations \
  "under:organizations/$DEST_ORG" \
  --organization="$SOURCE_ORG" --account="$NEW_ACCOUNT"

gcloud resource-manager org-policies allow \
  constraints/resourcemanager.allowedImportSources \
  "under:organizations/$SOURCE_ORG" \
  --organization="$DEST_ORG" --account="$NEW_ACCOUNT"
```

### Move

```bash
TOKEN=$(gcloud auth print-access-token --account="$NEW_ACCOUNT")
curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"destinationParent\":\"organizations/$DEST_ORG\"}" \
  "https://cloudresourcemanager.googleapis.com/v3/projects/$PROJECT_ID:move"
```

### Billing

```bash
gcloud billing projects link "$PROJECT_ID" \
  --billing-account="$NEW_BILLING" \
  --account="$NEW_ACCOUNT" --quiet

gcloud billing projects unlink "$PROJECT_ID" \
  --account="$NEW_ACCOUNT" --quiet
```

### Cleanup

```bash
gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
  --member="user:$OLD_ACCOUNT" \
  --role="roles/owner" \
  --account="$NEW_ACCOUNT" --quiet

gcloud resource-manager org-policies delete \
  constraints/resourcemanager.allowedExportDestinations \
  --organization="$SOURCE_ORG" --account="$NEW_ACCOUNT"

gcloud resource-manager org-policies delete \
  constraints/resourcemanager.allowedImportSources \
  --organization="$DEST_ORG" --account="$NEW_ACCOUNT"
```

### VM operations

```bash
gcloud compute instances stop "$VM" \
  --zone="$ZONE" --project="$PROJECT_ID" --account="$NEW_ACCOUNT"

gcloud compute instances start "$VM" \
  --zone="$ZONE" --project="$PROJECT_ID" --account="$NEW_ACCOUNT"
```

---

## 12. Cost notes

- A project move itself is free.
- The VM, disk, static IP, and bucket continue billing on the new account.
- Stopping the VM saves compute cost; the disk, static IP reservation, and
  bucket storage can still incur charges.
- The static IP remains reserved while the VM is stopped, so the same IP comes
  back on start.
- GCS storage charges continue even when the VM is stopped.
- Copying a large bucket can incur egress and operation charges; a project move
  does not.

---

## 13. If the old account is locked due to billing debt

This was a real concern in the s3-live-monitor migration. The short answer:

**A lock on the old account should not take away resources already moved to the
new account, provided the new billing and IAM are independent.**

Why:

- The VM/bucket project is parented to the new org.
- The project is linked to the new billing account.
- The old account has no project/bucket IAM.
- The old billing account has zero linked projects.

Check these shared dependencies:

1. **Payment method:** if the new billing account uses the same delinquent
   card/payment profile, new charges could fail. Use an independent valid
   payment method.
2. **Recovery email/phone:** if the new account's recovery contact is the old
   account, change it.
3. **Old orgs:** if the old account is locked before deletion, the empty orgs
   may remain; no data is lost.
4. **AWS credentials:** a Google lock does not affect AWS, but if the VM's AWS
   keys come from a locked AWS account, sync may stop. GCS data already stored
   remains.
5. **Old debt:** closing or losing the old account does not clear it. Settle it
   separately with Google Cloud Billing Support.

Do not delete the bucket or VM because of the old account's billing issue.

---

## 14. Evidence / audit considerations

For evidence or legal workloads:

- Record pre/post migration timestamps, project IDs, org IDs, and operator
  identities.
- Keep the metadata backup and `MIGRATION-PLAN.md`.
- Keep snapshot IDs and `SHA256SUMS`.
- Do not modify evidence objects during verification; use read-only checks.
- Export relevant Cloud Audit Logs if required.
- Record that the project move is metadata-only and that object bytes were not
  copied or rewritten.
- Keep old billing debt and account-closure records separate from evidence
  integrity records.

---

## 15. References

- Project migration overview:
  <https://cloud.google.com/resource-manager/docs/project-migration>
- Configure organization policies for migration:
  <https://cloud.google.com/resource-manager/docs/configure-org-policy>
- Resource Manager v3 `projects.move`:
  <https://cloud.google.com/resource-manager/reference/rest/v3/projects/move>
- Billing project link/unlink:
  <https://cloud.google.com/billing/docs/how-to/modify-project>
- Uniform bucket-level access:
  <https://cloud.google.com/storage/docs/uniform-bucket-level-access>
- Organization deletion:
  <https://cloud.google.com/resource-manager/docs/creating-managing-organization>
- Billing quota increase:
  <https://support.google.com/code/contact/billing_quota_increase>
- IAM troubleshooting:
  <https://cloud.google.com/iam/docs/troubleshooting-access>
- Policy Troubleshooter:
  <https://cloud.google.com/policy-intelligence/docs/troubleshoot-access>

---

## 16. Change history

| Date | Change |
|---|---|
| 2026-10-25 | Created from the s3-live-monitor account/project migration; includes real errors, fixes, and verification results |
