# Dashboard Fix Plan — Explorer + LiveFeed + Status + Live Sync + Exhaustive Decode

> Generated: 2026-09-01 (updated 2026-09-01 — added live S3→GCS sync + exhaustive deposit/withdrawal decode)
> Scope: Frontend fixes + live sync observability + full-bucket extraction with DB persistence (no misinformation)

---

## Phase 1: Explorer Folder Navigation Fix

### Problem
Clicking folders in the Explorer doesn't open/show data.

### Root Causes
1. Silent error swallowing hides API failures
2. No loading indicator after initial load
3. Race condition on rapid folder clicks
4. No active state highlight on selected folder

### Changes

#### `src/pages/Explorer.tsx`

**1a. Error handling in `refresh()` (lines 181-202)**
- Replace `.catch(() => {})` with `.catch((e) => console.error('[Explorer] refresh failed:', e))` at lines 189 and 196
- Add user-visible error state display when refresh fails

**1b. Loading state on navigation (line 476)**
- Change `{loading && initialLoad ? (` to `{loading ? (`
- This shows skeleton loading on every folder navigation, not just first load

**1c. Race condition fix**
- Add a `requestId` counter ref that increments on each `refresh()` call
- In `.then()` and `.finally()`, check if `requestId` matches before setting state
- Prevents stale API responses from overwriting newer navigation results

**1d. Sidebar active folder highlight**
- Pass current `prefix` to `FolderTree` component
- Add `active` class to folder item that matches current prefix
- Add `ex-folder-item-active` CSS class

#### `src/index.css`

**1e. Add active folder style**
```css
.ex-folder-item-active { background: var(--panel2); color: var(--accent); font-weight: 600; }
```

---

## Phase 2: LiveFeed Real-Time Deposit/Withdrawal Enhancement

### Problem
Live feed doesn't clearly show live deposits and withdrawals in real time.

### Changes

#### `src/pages/LiveFeed.tsx`

**2a. Add real-time summary stats**
Add a stats bar below controls showing:
- Count of deposit events
- Count of withdrawal events
- Total INR amount flowing through
- Count of wallet hits
- Connection status indicator

**2b. Enhance prefix filter**
Replace flat `<select>` with grouped `<optgroup>`:
- Money Flows: deposit/, withdrawal/
- Payment QRs: bankingqr/, qrcode/, payment/
- Communications: chat_file/
- Data: csv-files/, uploads/

**2c. Add event type badges to FeedCard**
- Green badge for `deposit/` prefix → "DEPOSIT"
- Red badge for `withdrawal/` prefix → "WITHDRAWAL"
- Blue badge for `bankingqr/` → "BANKING QR"
- Purple badge for `chat_file/` → "CHAT"
- Amount displayed prominently for money events

**2d. Add real-time summary bar**
```
⚡ 12 deposits (₹45,000) · 3 withdrawals (₹12,000) · 5 other · STREAMING
```

**2e. Improve FeedCard layout for money events**
- Show amount_inr prominently at top of card
- Show direction arrow (→ deposit, ← withdrawal)
- Highlight payment rail (UPI/bank/crypto) in card body

---

## Phase 3: Status Dashboard Improvements

### Problem
Overview page doesn't show real-time deposit/withdrawal data.

### Changes

#### `src/pages/Status.tsx`

**3a. Add live deposit/withdrawal stat cards**
Two new cards in the stat grid:
- 💰 Live Deposits: count + total INR (green accent)
- 💸 Live Withdrawals: count + total INR (red accent)

**3b. Add SSE connection for real-time updates**
- Connect to `/api/events` EventSource
- On `new_object` events with deposit/withdrawal prefix, increment counters
- On `enriched` events, refresh stats

**3c. Add live activity indicator**
- Show "LIVE" badge when SSE is connected
- Show last event timestamp
- Pulse animation on new events

#### `src/index.css`

**3d. Add CSS for new components**
- `.lf-summary-bar` - summary stats row
- `.lf-stat` - individual stat item
- `.deposit-badge` / `.withdrawal-badge` - event type colors
- `.st-card-live` - live indicator pulse

---

---

## Phase 4: Live Sync Observability (S3 → GCS)

### Problem
Dashboard must stay up-to-date with S3→GCS live sync without misinformation.

### Backend Reality (verified)
- `OBJECT_SOURCE=gcs` (`gs://cbtf-case-evidence-518405669914/s3-mirror/` via `config.py:23-26`).
- `GCSMirrorClient` (`backend/gcsclient.py:29`) prefixes `s3-mirror/`; `list_page(bucket, next_bucket)` parses `SubPrefixes`.
- `Collector.full_sweep()` / `poll_once()` (`backend/collector.py:44,117`) watermarks `wm:<prefix>` in DB.
- Sync check: `GET /api/sync` (`backend/app.py:577`) HEADs S3 object; Verify: `POST /api/verify` (size/etag/sha256); Reconcile: `scripts/reconcile_s3_gcs_shard.py` (sharded md5, 64 workers, non-destructive).
- Collection log: `intelligence/audit/collection-log.jsonl` (SHA-256 entries).

### Changes

#### `src/pages/Status.tsx` (4a)
- New header subtitle line: `Source: OBJECT_SOURCE | gcs_mirror: cbtf-case-evidence-518405669914/s3-mirror/ | Synced live`
- Add `Bucket Health` strip calling `GET /api/bucket-health` (already present) + `GET /api/sync?key=deposit/...` probe button.
- Surface `last_poll_utc`, `sweep`, `poll`, `enrich` states from `/api/status`.

#### `src/lib/api.ts` (4b)
- Add `BucketHealth` type (`mode/gcs_mirror/hot_prefixes/cold_prefix_count/last_*`).

#### `src/index.css` (4c)
- `.sync-strip` styles for source + health pills.

### No-misinformation guards
- Show `object source = gcs` label everywhere (not claimed as direct S3).
- Show `last_poll_utc` staleness warning if > 90s (poll) / > 130s (cold).

---

## Phase 5: Exhaustive Deposit/Withdrawal Decode (no missed objects, no re-decompile)

### Backend Reality (verified)
- Every object persisted in SQLite WAL `objects(key, etag, size, enriched, extract_json, ext, last_modified)` — `backend/db.py:63`.
- `upsert_objects()` re-enqueues when `etag`/`size` changes (`db.py:193`).
- Enrichment persisted `enriched=1`, `extract_json` (JSON), `ext`, `enrich_attempts`, `enrich_error` (`db.py:283`).
- `pending_enrichment(WALLET_FULL_SCAN)` has two queues: normal (qr/payment + csv .txt/.csv + image/audio) and full-scan (all `.txt` via `scan_prefix LIKE '%.txt%'`).
- Payment index: `payment_transactions`, `payment_flow_totals`, `payment_rail_summary` materialized via `rebuild_payment_index()` / `rebuild_payment_rail_summary()` on 45s/75s loops + SSE invalidation (`collector.py:369`, `app.py:777`).
- Amount sanitation: `MAX_AMOUNT_INR = 1_00_00_000` (`config.py:38`), `sanitize_stored_amounts()` (`db.py:306`).
- Both directions extracted: `direction IN ('deposit','withdrawal')` (`enrich.py:450,1051`), stored in `payment_transactions`.
- Confidence labeling: `file_path:line` provenance via `extract_json` fields + `fact_type` (`fact` vs `ai_lead`) downstream.

### Changes

#### `src/pages/Deposits.tsx` (5a)
- Already idempotent: `GET /api/money-flows` reads persisted `payment_transactions` view; SSE `enriched`/`new_object` triggers `refresh()` with 12s + on-demand. Add visible subtitle: `Persisted in SQLite WAL — enriched objects not re-decoded unless etag/size changes`.

#### `src/pages/Status.tsx` (5b)
- Add `Enrichment Queue` panel: `pending_enrichment` count via `GET /api/status` (`pending_enrich`) + link to run `POST /api/wallets/full-scan` when non-QR payments suspected.

#### Backend (no change required for exhaustive guarantee — documented)
- Ensure `ENRICH_MAX_AGE_HOURS=0` (unbounded) and `WALLET_FULL_SCAN=0` by default; expose `full-scan` toggle in UI rather than hard-coding.
- `reconcile_s3_gcs_shard.py` run is documented as verification step, not destructive.

### Acceptance (how to prove no miss)
- `GET /api/types` counts vs `payment_transactions` count.
- `GET /api/status` `pending_enrich` → 0 after sweep.
- `intelligence/audit/collection-log.jsonl` hashes match `.sha256`.

---

## Implementation Order

1. **Explorer.tsx** + **index.css** (Phase 1) — Fix folder navigation
2. **LiveFeed.tsx** + **index.css** (Phase 2) — Enhance live feed
3. **Status.tsx** + **index.css** (Phase 3) — Dashboard improvements (now includes Phase 4 observability & Phase 5 queue hint)
4. **api.ts** — Types for bucket-health + sync
5. **Verify** — Run `npm run build` to ensure no compilation errors

## Files Modified

| File | Phase | Description |
|------|-------|-------------|
| `src/pages/Explorer.tsx` | 1 | Fix folder navigation, error handling, loading state |
| `src/pages/LiveFeed.tsx` | 2 | Add summary counters, enhance filters, event badges |
| `src/pages/Status.tsx` | 3-5 | Add live deposit/withdrawal cards, SSE connection, sync strip, queue panel |
| `src/lib/api.ts` | 4 | BucketHealth type |
| `src/index.css` | 1-5 | CSS for active folders, badges, summary bar, sync strip |
