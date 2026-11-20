# Chain TX Verification Proposal
## Transaction hash column + Tronscan-verified wallet addresses

**Status:** Awaiting approval — no code changes made yet.
**Prepared:** 2026-11-20
**Target:** `s3-live-monitor` (Officer Query / Crypto wallets / CSV exports), bucket-2 dashboard `http://8.231.108.145:81`

---

## 1. Executive summary

The downloaded CSV (`all-slips-all-crypto-2026-11-01_000000Z-to-2026-11-20_235959Z.csv`)
has **35 crypto slips, 8 unique raw wallet addresses, and no transaction-hash column**.

I ran a dry-run analysis against the public Tronscan API using the OCR text already
stored in `objects.extract_json`. The results:

| Finding | Count |
|---|---|
| Slips in the file | 35 |
| Unique raw wallet addresses | 8 |
| Addresses that fail TRON base58check (OCR-corrupted) | **3 of 8** |
| Corrupted addresses that the existing OCR-repair code fixes to a valid, Tronscan-existing address | **3 of 3** |
| Slips with a full 64-hex txid already in the payload | 4 |
| Slips with a **partial** txid in the OCR text (`Hash: 00d1f557…34b31`) | 23 |
| Slips with any hash evidence (full or partial) | **32 of 35 (91%)** |
| Slips whose txid I resolved against Tronscan in the dry run | **26 of 35 (74%)** |
| Slips still unresolved (need deeper pagination, vision re-read, or genuinely no tx shown) | 9 |

The dry run also proved an important accuracy point: **the on-chain amount is often
different from the OCR amount.** Example: `deposit/1794504320060.jpg` OCR'd as
`5054 USDT`; the matching Tronscan transfer is **505 USDT**. The hash fragment
`00d1f557c42111f9…34b31` was decisive, and Tronscan supplied the correct amount,
from/to, time, and status.

**Proposal in one line:** add a server-side Tronscan resolver + cache, store the
canonical wallet address and transaction hash on every crypto slip, expose them in
the Officer Query table and CSV, and never fabricate a value when the match is
ambiguous.

---

## 2. What is wrong today (root causes)

### 2.1 Transaction hash is extracted but never stored on the slip

- `backend/enrich.py:158` `extract_txids()` only accepts **full 64-hex** hashes.
  It already handles line-wrapped hashes, but not the common Tronscan UI form
  `Hash: 00d1f557c42111f9…34b31`.
- `backend/db.py:1009` `_sync_wallet_hits()` writes `txids` into `wallet_hits`,
  but `backend/db.py:1891` `_replace_payment_rows()` does **not** write any txid
  into `payment_transactions`.
- The Officer Query list/export reads `payment_transactions`, so the hash never
  reaches the table or the CSV.

### 2.2 Wallet address repair is applied to `wallet_hits` but not to payment rails

- `backend/db.py:2586` `repair_txid_wallets()` recovers OCR-degraded TRON
  addresses and updates `objects.extract_json` + `wallet_hits`.
- It does **not** re-run `_replace_payment_rows()`, so `payment_transactions.rail_id`
  keeps the raw invalid address.
- Live proof: the Nov 1–20 window shows `TKrpTXoGqwWzrFXpyMSUnE84NFGjBEKzbG`
  (invalid) on 9 slips and `TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG` (valid) on 5 slips
  as **two separate wallets**, splitting one wallet's activity.

### 2.3 There is no chain lookup layer

- Tronscan URLs exist in `frontend/src/lib/explorers.ts`, but they are only
  deep-links. No server-side verification, no cached tx details, no
  fragment-to-full-hash reconstruction.
- The public Tronscan API works without a key for the endpoints we need:
  - `GET /api/transaction-info?hash=<txid>`
  - `GET /api/token_trc20/transfers?limit=50&start=<n>&sort=-timestamp&relatedAddress=<addr>`
  - `GET /api/account?address=<addr>`
- The new `/api/search/v2` endpoint requires an API key (401), so we should not
  depend on prefix search.

### 2.4 The current CSV is missing the hash and the canonical address

Current columns end at `fact_type`; there is no `transaction_hash`, no
`wallet_status`, no `tx_time`, no `tx_from`/`tx_to`. The `rail_id` column contains
the raw OCR value, including invalid checksums.

---

## 3. Proposed architecture

```
                        ┌─────────────────────────────────────────┐
                        │  Enrichment / OCR (existing)            │
                        │  objects.extract_json: wallets[], txids[]│
                        └───────────────┬─────────────────────────┘
                                        │
                    ┌───────────────────▼───────────────────┐
                    │  ChainResolver (new: backend/chain.py) │
                    │  1. canonicalise wallet (checksum/OCR) │
                    │  2. extract full + partial txids       │
                    │  3. match against Tronscan transfers   │
                    │  4. fetch tx details + verify status   │
                    └───────────────┬───────────────────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          │                         │                         │
   ┌──────▼──────┐          ┌───────▼───────┐        ┌───────▼────────┐
   │ wallet_     │          │ chain_tx_cache │        │ chain_links    │
   │ resolutions │          │ (Tronscan JSON)│        │ (per slip)     │
   └──────┬──────┘          └───────┬───────┘        └───────┬────────┘
          │                         │                         │
          └─────────────────────────┼─────────────────────────┘
                                    │
                    ┌───────────────▼───────────────────┐
                    │ payment_transactions (denormalised)│
                    │ txid, tx_status, tx_time, tx_from, │
                    │ tx_to, tx_amount, rail_id_raw …    │
                    └───────────────┬───────────────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          │                         │                         │
   ┌──────▼──────┐          ┌───────▼───────┐        ┌───────▼────────┐
   │ Officer     │          │ Wallets page   │        │ CSV exports    │
   │ Query table │          │ tx hash column │        │ new columns    │
   └─────────────┘          └───────────────┘        └────────────────┘
```

**Key design choice:** all Tronscan calls happen **server-side and asynchronously**.
The list/CSV endpoints only read SQLite. No browser CORS, no API key exposure,
no slow page loads, and every result is cached and auditable.

---

## 4. Data model changes (additive, safe migration)

### 4.1 `payment_transactions` — new columns

```sql
ALTER TABLE payment_transactions ADD COLUMN rail_id_raw TEXT NOT NULL DEFAULT '';
ALTER TABLE payment_transactions ADD COLUMN wallet_status TEXT NOT NULL DEFAULT '';
    -- valid | repaired | tronscan_confirmed | unresolved | unsupported
ALTER TABLE payment_transactions ADD COLUMN txid TEXT NOT NULL DEFAULT '';
ALTER TABLE payment_transactions ADD COLUMN txids TEXT NOT NULL DEFAULT '';
    -- JSON array of all validated txids for this slip
ALTER TABLE payment_transactions ADD COLUMN tx_status TEXT NOT NULL DEFAULT '';
    -- verified | derived | ambiguous | unresolved | none | pending
ALTER TABLE payment_transactions ADD COLUMN tx_method TEXT NOT NULL DEFAULT '';
    -- txid_full | txid_fragment | amount_time | counterparty_time | vision_reread
ALTER TABLE payment_transactions ADD COLUMN tx_confidence TEXT NOT NULL DEFAULT '';
ALTER TABLE payment_transactions ADD COLUMN tx_time TEXT NOT NULL DEFAULT '';
ALTER TABLE payment_transactions ADD COLUMN tx_from TEXT NOT NULL DEFAULT '';
ALTER TABLE payment_transactions ADD COLUMN tx_to TEXT NOT NULL DEFAULT '';
ALTER TABLE payment_transactions ADD COLUMN tx_amount REAL;
ALTER TABLE payment_transactions ADD COLUMN tx_token TEXT NOT NULL DEFAULT '';
ALTER TABLE payment_transactions ADD COLUMN tx_contract_ret TEXT NOT NULL DEFAULT '';
ALTER TABLE payment_transactions ADD COLUMN chain_checked_utc TEXT NOT NULL DEFAULT '';
```

Indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_pay_txid ON payment_transactions(txid) WHERE txid != '';
CREATE INDEX IF NOT EXISTS idx_pay_txstatus ON payment_transactions(tx_status);
```

### 4.2 `wallet_hits` — preserve the raw address

```sql
ALTER TABLE wallet_hits ADD COLUMN raw_address TEXT NOT NULL DEFAULT '';
ALTER TABLE wallet_hits ADD COLUMN wallet_status TEXT NOT NULL DEFAULT '';
ALTER TABLE wallet_hits ADD COLUMN resolved_utc TEXT NOT NULL DEFAULT '';
```

`address` becomes the canonical address; `raw_address` keeps the OCR value.
Backfill is a one-time pass over existing rows.

### 4.3 New table: `wallet_resolutions`

```sql
CREATE TABLE IF NOT EXISTS wallet_resolutions(
  raw_address TEXT PRIMARY KEY,
  resolved_address TEXT NOT NULL DEFAULT '',
  network TEXT NOT NULL DEFAULT 'TRON',
  method TEXT NOT NULL DEFAULT '',        -- checksum | ocr_repair | tronscan_account | prefix_match | vision_reread
  confidence TEXT NOT NULL DEFAULT '',
  tronscan_exists INTEGER NOT NULL DEFAULT 0,
  first_seen_utc TEXT NOT NULL DEFAULT '',
  checked_utc TEXT NOT NULL DEFAULT ''
);
```

### 4.4 New table: `chain_tx_cache`

```sql
CREATE TABLE IF NOT EXISTS chain_tx_cache(
  txid TEXT PRIMARY KEY,
  network TEXT NOT NULL DEFAULT 'TRON',
  from_address TEXT NOT NULL DEFAULT '',
  to_address TEXT NOT NULL DEFAULT '',
  amount REAL,
  token TEXT NOT NULL DEFAULT '',
  token_contract TEXT NOT NULL DEFAULT '',
  block_ts TEXT NOT NULL DEFAULT '',
  contract_ret TEXT NOT NULL DEFAULT '',
  confirmed INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT NOT NULL DEFAULT '',
  response_sha256 TEXT NOT NULL DEFAULT '',
  fetched_utc TEXT NOT NULL DEFAULT ''
);
```

### 4.5 New table: `chain_links` (audit + multiple candidates)

```sql
CREATE TABLE IF NOT EXISTS chain_links(
  source_key TEXT NOT NULL,
  rail_id TEXT NOT NULL,
  txid TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',        -- verified | derived | ambiguous
  method TEXT NOT NULL DEFAULT '',
  confidence TEXT NOT NULL DEFAULT '',
  match_score REAL NOT NULL DEFAULT 0,
  from_address TEXT NOT NULL DEFAULT '',
  to_address TEXT NOT NULL DEFAULT '',
  amount REAL,
  token TEXT NOT NULL DEFAULT '',
  block_ts TEXT NOT NULL DEFAULT '',
  contract_ret TEXT NOT NULL DEFAULT '',
  candidates_json TEXT NOT NULL DEFAULT '',
  checked_utc TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(source_key, rail_id, txid)
);
CREATE INDEX IF NOT EXISTS idx_chain_links_key ON chain_links(source_key);
```

Why a separate table: an officer can see **all** candidates for an ambiguous slip,
not just the one the resolver picked. The denormalised columns on
`payment_transactions` keep list/CSV queries fast.

---

## 5. Resolver logic

### 5.1 Canonicalise the wallet address

Input: raw address from `payment_transactions.rail_id` / `payload.wallets[].address`.

1. If 34 chars and `_tron_checksum_ok()` → `valid`.
2. If 34 chars and checksum fails → `recover_tron_ocr()` (existing, `backend/enrich.py:350`).
   - If a checksum-valid variant is found → `repaired`; keep `rail_id_raw`.
   - If multiple valid variants → `unresolved` (never guess).
3. If shorter than 34 / contains `…` / is a display fragment:
   - Match prefix+suffix against known addresses in `wallet_hits`, the LE master
     list, and the case census. If exactly one match → `repaired` (method `prefix_match`).
   - Else mark `unresolved` and queue for vision re-read (Phase 3).
4. If the network is not TRON → `unsupported` (EVM/BTC adapter later).
5. Optional confirmation: `GET /api/account?address=<canonical>`.
   - Account exists → `tronscan_confirmed` (strong).
   - Account does not exist but checksum is valid → still canonical; mark
     `valid` (a zero-activity address is still a valid address).

**Dry-run proof:** all three corrupted addresses in the sample repair cleanly and
exist on Tronscan:

| Raw OCR address | Repaired address | Tronscan account |
|---|---|---|
| `TB4rLHLJSWNBR8z7BSPbdrT9qFNuP4rgyE` | `TB4rLHLJ5wNBR8z7BSPbdrT9qFNuP4rgyE` | exists (179 tx out) |
| `TKrpTXoGqwWzrFXpyMSUnE84NFGjBEKzbG` | `TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG` | exists (89 tx out) |
| `TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWys` | `TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWyS` | exists (749 tx out) |

### 5.2 Extract the transaction hash

Add a new `extract_txid_fragments(text)` alongside the existing
`extract_txids()`:

- Full 64-hex → `full`.
- Tronscan display form: `Hash: <hex8+> … <hex4+>` → `prefix`, `suffix`.
- Wrapped / OCR-spaced hashes → join 20+ hex runs, as `extract_txids()` already does.
- OCR-noise normalisation for matching only: `l/I → 1`, `O → 0`, `£ → c`,
  strip spaces/ellipsis. Keep the raw string for the audit trail.
- Never treat a fragment as a full hash.

### 5.3 Match a full hash

1. `GET /api/transaction-info?hash=<txid>`.
2. If `contractRet=SUCCESS` and the tx involves the canonical wallet
   (`trc20TransferInfo.from_address` or `to_address`) → `verified`.
3. If the tx exists but does not involve the wallet → `ambiguous` (store it, do not
   attach it as the slip's hash).
4. If Tronscan returns `{}` → try OCR-normalised variants (bounded, e.g. 20),
   then mark `unresolved`.

### 5.4 Match a partial hash (the high-value path)

For each canonical wallet on the slip:

1. Fetch TRC20 transfers for the wallet, newest first:
   `GET /api/token_trc20/transfers?limit=50&start=<n>&sort=-timestamp&relatedAddress=<addr>`
2. Paginate until `block_ts < slip_time - window` (default window: 24h before,
   2h after; expand to 72h only if no match).
3. Filter:
   - token is USDT (contract `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`), or the
     token shown on the slip.
   - direction matches: deposit → wallet is `to_address`; withdrawal → wallet is
     `from_address`.
   - `txid.startswith(prefix)` and/or `txid.endswith(suffix)`.
4. Score:
   - prefix match: +2
   - suffix match: +2
   - amount match (within 0.5%): +2
   - counterparty address match: +3
   - time proximity: +0..2
5. If exactly one candidate has a fragment match → `derived`, confidence `high`.
6. If multiple → rank; if the top score is clearly ahead and amount+direction
   agree → `derived`, confidence `medium`; otherwise `ambiguous` (store all
   candidates; show "N candidates" in the UI).
7. If none → `unresolved`; queue for a wider window / vision re-read.

**Dry-run proof (4 examples):**

| Slip | Raw address | Canonical address | Resolved txid | On-chain amount | Method |
|---|---|---|---|---|---|
| `deposit/1794504320060.jpg` | `TLwod…59Hdc` (valid) | same | `00d1f557c42111f9653dab34e0f2b7e70cd74405fa33cd48c5504e7106734b31` | 505 USDT (OCR said 5054) | fragment |
| `withdrawal/1794488122413.jpg` | `TKrp…wWzr…` (invalid) | `TKrp…qWWzr…` | `833f414cdb6a8b7a1a643532cfb298562b834191f20c9e2842e84da58075e9e4` | 1537 USDT | fragment |
| `withdrawal/1794420534629.jpg` | `TKrp…qWWzr…` (valid) | same | `fc6422f08c4933f4bdfdce81435aa483edcaba0c4a6b5da868c5258f6b4ec705` | 891 USDT | amount + direction + time (unique) |
| `withdrawal/1793867596316.jpg` | `TB4r…P4rgyE` (invalid) | `TB4r…LJ5w…` | `4a620d0cbc9b4e5e0cf259d544c8da99b7bd96e9c978ac5466c7e764feeeb48a` | 1019 USDT | fragment |

### 5.5 No hash at all

- If a USDT amount + direction + time window yields exactly one transfer → `derived`
  (confidence `medium`), method `amount_time`.
- If a counterparty address is present and the transfer involves both addresses →
  `derived` (confidence `high`), method `counterparty_time`.
- If zero or multiple → `unresolved` or `ambiguous`. **Never invent a hash.**

### 5.6 Fetch details and store

For the chosen txid:

- `GET /api/transaction-info?hash=<txid>` → `trc20TransferInfo[0]` gives
  `from_address`, `to_address`, `amount_str`, `decimals`, `symbol`;
  `timestamp`, `contractRet`, `confirmed`.
- Store in `chain_tx_cache` + `chain_links`.
- Update `payment_transactions` denormalised columns.
- Store `response_sha256` + `fetched_utc` for reproducibility.

---

## 6. API changes

### 6.1 Extend existing responses

`GET /api/money-flows` — each item gains:

```json
{
  "rail_id_raw": "TKrpTXoGqwWzrFXpyMSUnE84NFGjBEKzbG",
  "wallet_status": "repaired",
  "txid": "833f414cdb6a8b7a1a643532cfb298562b834191f20c9e2842e84da58075e9e4",
  "txids": ["833f414c…e9e4"],
  "tx_status": "derived",
  "tx_method": "txid_fragment",
  "tx_confidence": "high",
  "tx_time": "2026-11-12T12:48:39Z",
  "tx_from": "TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG",
  "tx_to": "TTAUPfk4Cqi17rTuZZRs3i1EmAsp4WYiKC",
  "tx_amount": 1537.0,
  "tx_token": "USDT",
  "tx_contract_ret": "SUCCESS",
  "tronscan_tx_url": "https://tronscan.org/#/transaction/833f414c…e9e4",
  "tronscan_address_url": "https://tronscan.org/#/address/TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG",
  "chain_checked_utc": "2026-11-20T08:00:00Z"
}
```

`GET /api/money-flows-unique` — crypto rows gain `txids`, `tx_count`,
`verified_tx_count`, `wallet_status`, `tronscan_url`.

`GET /api/wallets` — already returns `txids`; add `txid`, `tx_status`,
`wallet_status`, `raw_address`.

### 6.2 New endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/chain/status` | Counts by `tx_status`, queue depth, last run, API health |
| `POST /api/chain/verify` | Body `{since, until, source_keys?, limit, dry_run}` — bounded backfill |
| `POST /api/chain/verify-one` | Re-verify one `source_key` (UI button) |
| `GET /api/chain/candidates/{source_key}` | All candidate txids for an ambiguous slip |
| `GET /api/chain/tx/{txid}` | Cached tx details + Tronscan URL |

All require the existing `auth` dependency. `verify` defaults to `dry_run=1` for
safety; the UI/admin passes `dry_run=0` explicitly.

### 6.3 Status integration

Add a `chain` block to `GET /api/status`:

```json
"chain": {
  "enabled": true,
  "verified": 4,
  "derived": 26,
  "ambiguous": 2,
  "unresolved": 9,
  "pending": 150,
  "last_run_utc": "...",
  "api_ok": true,
  "api_last_error": ""
}
```

---

## 7. Website changes

### 7.1 Officer Query → Every slip table

Current columns: Bucket time · Indexed · Amount · UTR · Wallet address · Network ·
Source object.

Proposed: add **Transaction hash** after Wallet address, and a chain-status badge.

```
| Bucket time | Indexed | Amount | UTR | Wallet address | Transaction hash | Network | Source object |
| 2026-11-12 17:25 | 17:25 | — | — | TLwod…59Hdc ✓ | 00d1f557…34b31 ✓ Tronscan tx | TRON | deposit/…jpg |
| 2026-11-12 12:55 | 12:56 | — | — | TKrp…qWWzr… (OCR-corrected) | 833f414c…e9e4 ~ derived | TRON | withdrawal/…jpg |
| 2026-11-11 18:08 | 18:09 | — | — | TKrp…qWWzr… | fc6422f0…ec705 ~ derived | TRON | withdrawal/…jpg |
```

Behaviour:

- **Verified** (full hash confirmed by Tronscan): green `✓` badge; hash truncated
  to `first 8…last 6`; full hash in `title`; copy button; `Tronscan tx ↗` link.
- **Derived** (fragment / amount+time match): amber `~` badge with tooltip
  `Derived from hash fragment 00d1f557…34b31 + wallet + time`; same link/copy.
- **Ambiguous**: `2 candidates` button opens an expander listing each candidate
  with time/amount/from/to and a Tronscan link; no primary hash is asserted.
- **Unresolved**: `—` + `Find on Tronscan` link (address view) + `Re-verify` button.
- **Multiple txids**: show first + `+N`; expander lists all.
- **Wallet cell**: show canonical address; if repaired, a small `OCR-corrected`
  badge with the raw value in the tooltip. Link to Tronscan address.
- **Amount cell**: if `tx_amount` differs from the OCR amount, show
  `on-chain 505 USDT` with a warning badge `OCR 5,054` in the tooltip.

Add a **Chain status** filter to the Officer Query panel: `All / Verified / Derived /
Ambiguous / Unresolved`. This lets an officer focus on the 9 unresolved slips.

### 7.2 Unique addresses view

- Show canonical address (merged variants).
- Add a **Tx hashes** column: `26 tx · 18 verified` with a sample hash link.
- The CSV unique export gains `txids`, `tx_count`, `verified_tx_count`,
  `wallet_status`, `tronscan_url`.

### 7.3 Crypto wallets page

- Add a **Tx hash** column: first txid + `+N`; Tronscan tx link.
- Add a `verified` badge when `tx_status=verified`.
- Keep the existing Arkham / Tronscan / TraceDesk links.

### 7.4 Shared components

- `frontend/src/lib/explorers.ts` already has `tronscanTxUrl()`, `firstTxid()`,
  `bestTronscanUrl()`. Reuse them.
- Add a small `<TxHashCell txid txids status ... />` component and a
  `<ChainDetails />` expander to avoid duplicating logic between pages.
- Copy-to-clipboard pattern already exists in `Preview.tsx:76`.

---

## 8. CSV changes

Keep the existing 19 columns **in the same order** (backward compatible), then
append:

| # | New column | Example |
|---|---|---|
| 20 | `wallet_address_raw` | `TKrpTXoGqwWzrFXpyMSUnE84NFGjBEKzbG` |
| 21 | `wallet_address_resolved` | `TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG` |
| 22 | `wallet_status` | `repaired` |
| 23 | `transaction_hash` | `833f414cdb6a8b7a1a643532cfb298562b834191f20c9e2842e84da58075e9e4` |
| 24 | `transaction_hashes_all` | `833f414c…e9e4` (pipe-separated if >1) |
| 25 | `tx_status` | `derived` |
| 26 | `tx_method` | `txid_fragment` |
| 27 | `tx_confidence` | `high` |
| 28 | `tx_time_utc` | `2026-11-12T12:48:39Z` |
| 29 | `tx_from_address` | `TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG` |
| 30 | `tx_to_address` | `TTAUPfk4Cqi17rTuZZRs3i1EmAsp4WYiKC` |
| 31 | `tx_amount` | `1537` |
| 32 | `tx_token` | `USDT` |
| 33 | `tx_contract_ret` | `SUCCESS` |
| 34 | `tronscan_tx_url` | `https://tronscan.org/#/transaction/833f…e9e4` |
| 35 | `tronscan_address_url` | `https://tronscan.org/#/address/TKrp…` |
| 36 | `chain_checked_utc` | `2026-11-20T08:00:00Z` |
| 37 | `tx_candidates` | `txid@time@amount` (pipe-separated, ambiguous only) |

`fact_type` policy (needs your decision):

- `verified` → `fact`
- `derived` → `fact` if you accept on-chain reconstruction as fact; otherwise `ai_lead`
- `ambiguous` / `unresolved` → `ai_lead`

I recommend `fact` for `verified`, `ai_lead` for everything else, plus the explicit
`tx_status`/`tx_confidence` columns so the distinction is never lost.

### Sample CSV rows (proposed)

```csv
bucket_last_modified_utc,indexed_seen_utc,direction,...,rail_id,...,fact_type,wallet_address_raw,wallet_address_resolved,wallet_status,transaction_hash,tx_status,tx_method,tx_confidence,tx_time_utc,tx_from_address,tx_to_address,tx_amount,tx_token,tx_contract_ret,tronscan_tx_url,tronscan_address_url,chain_checked_utc
2026-11-12T17:25:21Z,2026-11-12T17:25:24Z,deposit,...,TLwodKP4VM3gwx3GfPeuaGv7oHAwo59Hdc,...,fact,TLwodKP4VM3gwx3GfPeuaGv7oHAwo59Hdc,TLwodKP4VM3gwx3GfPeuaGv7oHAwo59Hdc,valid,00d1f557c42111f9653dab34e0f2b7e70cd74405fa33cd48c5504e7106734b31,derived,txid_fragment,high,2026-11-12T17:20:39Z,TLwodKP4VM3gwx3GfPeuaGv7oHAwo59Hdc,TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG,505,USDT,SUCCESS,https://tronscan.org/#/transaction/00d1…4b31,https://tronscan.org/#/address/TLwod…59Hdc,2026-11-20T08:00:00Z
2026-11-12T12:55:23Z,2026-11-12T12:56:28Z,withdrawal,...,TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG,...,fact,TKrpTXoGqwWzrFXpyMSUnE84NFGjBEKzbG,TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG,repaired,833f414cdb6a8b7a1a643532cfb298562b834191f20c9e2842e84da58075e9e4,derived,txid_fragment,high,2026-11-12T12:48:39Z,TKrpTXoGqWWzrFXpyMSUnE84NFGjBEKzbG,TTAUPfk4Cqi17rTuZZRs3i1EmAsp4WYiKC,1537,USDT,SUCCESS,https://tronscan.org/#/transaction/833f…e9e4,https://tronscan.org/#/address/TKrp…qWWzr…,2026-11-20T08:00:00Z
```

---

## 9. Backfill, worker, and rate limits

### 9.1 Corpus size (live, bucket-2)

- `payment_transactions` crypto rows: **191**
- distinct crypto rails: **25**
- `wallet_hits`: **283** rows / **45** distinct addresses
- This is small. A full backfill is a few minutes, not hours.

### 9.2 Worker

- New `ChainVerifier` daemon thread started in `app.startup()` after the
  enrichment worker; stopped in `shutdown()`.
- Every 30s: pick up to 10 crypto slips with `tx_status=''` or `tx_status='pending'`
  and `last_modified` newest-first; resolve; sleep.
- New enrichment writes set `tx_status='pending'` so fresh slips are picked up.
- `repair_txid_wallets()` will call `_replace_payment_rows()` so repaired addresses
  propagate to `payment_transactions`.
- Low priority: if the enrichment backlog is large, the worker can pause
  (`S3LM_CHAIN_PAUSE_WHILE_BACKLOG=1`).

### 9.3 Rate limits and caching

- Global token bucket: **3 requests/second** (configurable `S3LM_CHAIN_RATE`).
- Per-address transfer pages cached for **1 hour**; tx details cached for **24 hours**
  (immutable except status).
- Retries: 2 with exponential backoff (0.5s, 2s) on 429/5xx.
- Circuit breaker: after 10 consecutive failures, pause 10 minutes and surface
  `api_ok=false` in `/api/chain/status`.
- Optional `TRON-PRO-API-KEY` header if you have a Tronscan key; the public API
  works without it for our volume.
- Max 20 pages per address per slip; if not found, mark `unresolved` and retry
  later with a wider window.

### 9.4 Backfill sequence

1. **Phase 1 (no network):** copy full txids from `objects.extract_json` /
   `evidence_json` into `payment_transactions.txid/txids`; set `tx_status='pending'`
   for rows with hash evidence.
2. **Phase 2 (network):** canonicalise all crypto addresses; resolve all pending
   rows; store `chain_links` / `chain_tx_cache`.
3. **Phase 3 (vision):** for unresolved rows with a hash fragment, re-read the
   source image with Gemini/Groq; verify the returned hash/address via Tronscan.
4. Re-run the unique counts; verify the 8 raw wallets collapse to 7 canonical
   wallets in the sample window.

---

## 10. Accuracy and audit safeguards

1. **Never overwrite evidence.** Keep `rail_id_raw`, `evidence_json`, and the
   original OCR text. The canonical value is a separate field.
2. **No fabrication.** A txid is attached only when:
   - it is a full hash confirmed by Tronscan and involves the wallet; or
   - it is a unique fragment match (prefix+suffix) involving the wallet; or
   - it is a unique amount+direction+time match, explicitly labelled `derived`.
3. **Ambiguity is visible.** Multiple candidates are stored and shown; the UI
   says "N candidates", not a single hash.
4. **Provenance is stored.** `tx_method`, `tx_confidence`, `chain_checked_utc`,
   `response_sha256`, and the Tronscan URL are in every row/CSV.
5. **On-chain amount is authoritative, but displayed beside the OCR amount.**
   We do not silently change `amount_inr` (INR totals are unaffected because
   crypto slips have `amount_inr=null`). The new `tx_amount` column is the
   on-chain value; the UI flags discrepancies.
6. **TRON only for now.** Non-TRON wallets are marked `unsupported`; no
   cross-chain guessing.
7. **Audit trail.** `chain_links` keeps every candidate ever seen, not just the
   chosen one. The CSV exposes `tx_candidates` for ambiguous rows.
8. **Reproducibility.** Raw Tronscan JSON + SHA-256 + fetch time are stored in
   `chain_tx_cache`, so an examiner can re-check the exact response.

---

## 11. Test plan

### Unit tests (no network)

- `extract_txid_fragments()`: full, `Hash: abc…def`, wrapped lines, OCR noise,
  ellipsis, false positives (wallet address, contract address).
- `_recover_tron_ocr()` on the 3 known bad addresses → exact expected repairs.
- Matcher scoring: prefix/suffix/amount/direction/counterparty; unique vs ambiguous.
- No-fabrication: a slip with no hash and multiple same-amount transfers must
  produce `ambiguous`, not a chosen hash.
- Amount discrepancy: OCR 5054 vs on-chain 505 → `tx_amount=505`, flag set.
- Canonicalisation: raw `TKrp…w…` and `TKrp…W…` merge to one wallet in the unique view.

### Integration tests (recorded fixtures)

- Record real Tronscan responses for the 4 example txids and 3 addresses; replay
  them in CI. No live network in tests.
- `POST /api/chain/verify?dry_run=1` on the 35-row window returns the expected
  status counts (verified/derived/ambiguous/unresolved).
- CSV export contains all 37 columns and the exact expected values for the
  4 example rows.
- Idempotency: running the resolver twice does not duplicate `chain_links` rows.

### Live acceptance

- On :81, open Officer Query → Crypto → Nov 1–20.
- The table shows a Transaction hash column; the 3 corrupted addresses display
  canonical values with an `OCR-corrected` badge.
- Download the CSV; open in Excel; verify the new columns and the 4 example rows.
- `/api/chain/status` shows `verified + derived + ambiguous + unresolved = 35`.
- Re-run the backfill; counts are stable.

---

## 12. Rollout plan and effort

| Phase | Work | Effort |
|---|---|---|
| **1. Foundation** | Schema migration; copy txids from payloads; canonicalise addresses; CSV + table hash column; Wallets page hash column | 1.5 days |
| **2. Resolver** | `backend/chain.py`; Tronscan client; fragment extractor; matcher; cache tables; worker; endpoints; status | 2.5 days |
| **3. UI polish** | Badges, expander, candidates, re-verify button, chain filter, amount discrepancy | 1 day |
| **4. Vision fallback** | Gemini/Groq targeted re-read for unresolved rows; bounded budget | 1 day (optional) |
| **5. Tests + deploy** | Unit/integration tests; backfill on :81; verify; ship | 1 day |
| **Total** | | **~6–7 working days** |

I recommend shipping **Phase 1 first** (it fixes the visible CSV/table problem
immediately with zero network risk), then Phase 2–3 behind a feature flag.

---

## 13. Deployment and rollback

- **Migration:** additive `ALTER TABLE` only; runs automatically via
  `db._migrate()` on startup. No destructive changes.
- **Feature flag:** `S3LM_CHAIN_VERIFY=1` enables the worker; default `0` until
  the backfill is verified. The UI still shows existing full txids in Phase 1.
- **Deploy:** `./deploy/ship.sh "feat: tronscan tx verification"` (commit → push →
  VM git pull + cached rebuild + health check).
- **Target:** :81 (`s3lm-b2`) first, because that is the dashboard in the
  screenshot and the CSV source. Then :80 / :82 if you want the same feature
  everywhere (the code is shared; each instance runs its own DB/backfill).
- **Backfill:** `POST /api/chain/verify?since=2026-07-01&until=2026-11-20&limit=500&dry_run=0`
  (or a one-off CLI wrapper). Monitor `/api/chain/status`.
- **Rollback:** set `S3LM_CHAIN_VERIFY=0` and redeploy; the new columns are
  additive and harmless. No data loss.

---

## 14. Decisions needed from you

1. **Address display:** replace `rail_id` with the canonical address and add
   `wallet_address_raw` (my recommendation), or keep `rail_id` raw and add
   `wallet_address_resolved`? Replacing merges duplicate wallets and fixes the
   unique counts; keeping raw is less invasive.
2. **`fact_type` for derived matches:** `fact` or `ai_lead`? I recommend `fact`
   only for full-hash verified, `ai_lead` for derived/ambiguous.
3. **Auto-link amount-only matches:** allow unique amount+direction+time matches
   (labelled `derived`, medium confidence), or require a hash fragment for any
   auto-link? My dry run resolved 2 extra slips this way; both were exact and
   direction-correct.
4. **Scope:** TRON only for now (recommended), or also EVM/BTC? The sample is
   100% TRON.
5. **Vision re-read:** spend Gemini/Groq budget on the 9 unresolved slips (and
   future unresolved rows)? If yes, cap at e.g. 50 images/day.
6. **Deploy target:** :81 only first, or all three dashboards?
7. **Tronscan API key:** do you have one? Not required, but it raises rate limits.
8. **CSV column names:** are the proposed names acceptable, or do you want a
   specific naming convention (e.g. `tx_hash` vs `transaction_hash`)?

Once you approve, I will implement Phase 1 first, show you the updated CSV/table
on :81, then proceed to the resolver.

---

## Appendix A — Evidence used for this proposal

- Downloaded CSV: `C:\Users\prism\Downloads\all-slips-all-crypto-2026-11-01_000000Z-to-2026-11-20_235959Z.csv`
  (35 rows, 19 columns, no tx hash).
- Live API: `http://8.231.108.145:81/api/money-flows?direction=both&rail_type=crypto_wallet&folder=all&since=2026-11-01&until=2026-11-20`
  (35 rows, 8 raw rails, 191 crypto rows total).
- Live API: `http://8.231.108.145:81/api/wallets?limit=300`
  (283 hits, 45 distinct wallets, 49 hits with txids).
- Tronscan public API dry run:
  - `transaction-info` for the 4 example txids.
  - `token_trc20/transfers` for the 8 addresses, paginated.
  - `account` for the 3 repaired addresses (all exist).
- Existing code:
  - `backend/enrich.py:126` `TXID_RE`, `:158` `extract_txids`, `:350` `_recover_tron_ocr`
  - `backend/db.py:364` `payment_transactions`, `:1009` `_sync_wallet_hits`,
    `:1891` `_replace_payment_rows`, `:2586` `repair_txid_wallets`
  - `backend/app.py:2716` `_MONEY_FLOW_EXPORT_COLUMNS`, `:2806` export
  - `frontend/src/pages/OfficerQuery.tsx:811` slips table
  - `frontend/src/pages/Wallets.tsx:355` wallets table
  - `frontend/src/lib/explorers.ts` Tronscan URL helpers
