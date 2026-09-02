# Monitoring Dashboard Improvement and Deployment Plan

Updated: 2026-09-02 UTC
Case: `CASE-2026-CBTF`
Authorization: `STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING`

## Outcome

Make the dashboard an honest, measurable view of three different stages:

1. the current public S3 source inventory;
2. the retained GCS evidence mirror;
3. the locally indexed and decoded evidence available to investigators.

The UI must never imply these counts are identical or that OCR is exhaustive while a backlog or capture exception remains.

## Review findings

| Priority | Finding | Effect | Resolution |
|---|---|---|---|
| P0 | GCS preservation had stopped receiving new S3 objects | Dashboard was frozen even while its collector was healthy | Run a continuous, read-only S3 LIST/GET to GCS upload daemon |
| P0 | User-prefixed keys are not time ordered | Lexicographic watermarks permanently missed keys uploaded before the watermark | Notify the dashboard after every successful mirror upload; retain a wrapping cold crawl |
| P0 | Cold crawl treated any known key as unchanged | Same-key replacements were not re-indexed | Compare size, ETag and normalized timestamp |
| P1 | Crawl cap counted only unknown objects | A caught-up crawl could scan an entire million-object keyspace | Bound objects visited and persist the cursor across restarts |
| P1 | Mixed RFC-1123 and ISO timestamps | Newest-first views surfaced old evidence | Normalize timestamps at sync, GCS client and DB repair layers |
| P1 | Sync showed cumulative scans and GCS total only | Operators could not distinguish current source from preserved history | Persist per-prefix S3 inventory and show current S3, GCS, retained-only estimate and exceptions separately |
| P1 | Sync health check used unavailable `pgrep` | Healthy daemon appeared unhealthy | Check `/proc/1/cmdline` in the slim container |
| P1 | Confirmed 404 tombstones retried every cycle | Repeated requests and duplicate deletion records | Mark confirmed deletions terminal; retry transient failures only |
| P1 | OCR totals lacked coverage/rate/ETA | Partial extraction looked complete | Show eligible coverage, pending, hourly rate, ETA and errors |
| P2 | API token was added to every request URL | Token could leak through access logs/history | Use bearer headers for fetch/POST; query token remains only where browser APIs cannot set headers (SSE/media) |
| P0 | Receipts with both payer and payee identifiers promoted every rail to the mule side | Gambler VPAs/accounts could be falsely ranked as criminal receiving/sending rails | Extract sender/beneficiary roles, exclude explicitly opposite rails, and downgrade ambiguous legacy evidence |
| P1 | Deposit and withdrawal evidence had no consolidated rail view | Analysts could not see rails operating on both collection and payout sides | Add ranked suspected-mule aggregation with direction counts, linked gamblers, evidence provenance and analyst-conclusion labels |

## Implementation phases

### Phase A — source-to-mirror correctness

- Maintain a disk-backed sync manifest (`state.db`) using key, size and S3 ETag.
- Stream every new/changed object through SHA-256 and upload with source metadata.
- Keep status and deletion evidence outside `s3-mirror/` so synthetic objects never enter evidence counts.
- Publish a starting, syncing and post-drain status snapshot.
- Record a complete source inventory per prefix and expose its oldest measurement time.
- Push metadata to `POST /api/sync/object` only after GCS upload succeeds, closing the watermark latency gap.

Acceptance:

- sync container healthy;
- no upload errors or unresolved transient tombstones;
- `queued_pending=0` after each drain;
- all discovered source prefixes have an inventory measurement;
- newest S3 test key appears in the manifest within one sync cycle;
- GCS metadata contains source key, ETag, SHA-256, normalized LastModified, case and authorization reference.

### Phase B — manifest correctness and dashboard truthfulness

- Persist wrapping crawl cursors in SQLite.
- Bound each crawl by visited objects.
- Re-index same-key replacements and reset their extraction state.
- Display current S3 count separately from preserved GCS and indexed local count.
- Label preserved-only count as an estimate until every prefix inventory is complete.
- Display sync heartbeat age, current-cycle scan, fast-path notification count, capture exceptions and inventory age.

Acceptance:

- unit tests prove a changed ETag is detected;
- unit tests prove a 1,000-object cap performs one 1,000-object page visit;
- newest-first chat and withdrawal views return ISO timestamps and current evidence;
- dashboard does not label bucket failure when only the access session is stale.

### Phase C — extraction completeness and performance

- Keep raw object discovery independent from OCR/QR processing.
- Process newest payment evidence first with four spawned CPU workers on the
  4-vCPU VM; a drain lock prevents duplicate batches from concurrent loops.
- Persist `enriched_utc`, extraction payload, retry error and content version.
- Requeue only when size, ETag or timestamp changes.
- Treat completion as `pending=0` plus reviewed errors, not merely “sweep done”.

Acceptance:

- coverage, last-hour throughput and ETA are visible;
- payment totals exclude rejected implausible amounts;
- deposit and withdrawal regression tests pass;
- materialized payment summaries agree with normalized rows.

### Phase C2 — role-aware mule-rail intelligence

- Interpret a deposit as `gambler → beneficiary/receiving rail`.
- Interpret a withdrawal as `sender/sending rail → gambler`.
- Record `rail_role` and `role_confidence` for UPI IDs, bank accounts and crypto wallets.
- Exclude rails explicitly labelled as the opposite participant; retain unknown-role legacy evidence as low-confidence leads.
- Aggregate each rail across both directions with transaction, linked amount,
  distinct gambler, first/last-seen and object/reference evidence counts.
- Rank candidates for analyst triage using a transparent heuristic. Keep observed
  metrics as `fact` and candidate priority as `analyst_conclusion`.

Acceptance:

- regression tests prove payer VPAs do not appear as deposit receiving rails;
- regression tests prove beneficiary VPAs do not appear as withdrawal sending rails;
- a rail observed in both directions is shown once with both sides quantified;
- every candidate links back to exact object evidence or named reference provenance;
- UI explicitly warns that a candidate score is not criminal attribution.

### Phase D — deployment, IAM and operations

Required VM configuration:

- machine: `e2-standard-4` (4 vCPU, 16 GB RAM);
- boot disk: 40 GB balanced persistent disk recommended (20 GB was already 60% used);
- emergency swap: 2 GB;
- application memory limit: 4 GB;
- sync memory limit: 600 MB.

Required service-account access:

- VM scope: `cloud-platform`;
- GCS evidence bucket: object create/read plus metadata/status updates. `roles/storage.objectAdmin` is currently sufficient; a future custom least-privilege role should omit delete;
- no AWS credential is required because the authorized source is publicly listable/readable;
- no S3 mutation is performed.

Network controls:

- expose HTTP only where investigators require it;
- retain the dashboard access gate;
- restrict firewall source ranges or add TLS/IAP when investigator CIDRs/identity policy are available.

Deployment verification:

1. build Python and frontend artifacts;
2. run backend regression tests and Compose validation;
3. deploy while preserving Docker volumes and the access token;
4. verify both container health states;
5. verify authenticated status, mirror sync, newest chat, newest withdrawal and money-flow endpoints;
6. compare live source and mirror on sampled/sharded reconciliation;
7. record hashes, UTC times and results under `intelligence/`.

## Remaining operational work

- The retained GCS mirror can legitimately contain objects no longer present in current S3; do not delete them to force equal totals.
- A complete current-source inventory is only authoritative when all prefixes are measured and the oldest measurement is within the operational threshold.
- Full historical OCR remains compute-bound. VM expansion beyond 4 vCPU should be based on measured throughput/ETA after this release, not bucket count alone.
- Replace URL tokens for SSE/media with an HttpOnly session cookie when TLS is introduced.
- Restrict public firewall source ranges once investigator egress CIDRs are supplied.
