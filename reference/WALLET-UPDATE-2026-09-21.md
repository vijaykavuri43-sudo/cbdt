# CBTF wallet update — 2026-09-21 document

**Processed:** 2026-10-25
**Source:** `C:\Users\prism\Downloads\CBTF_Crypto_Wallets_Updated-21-09-26.docx`
**Compared with:** `CBTF_Crypto_Wallets_Updated-14-09-26.docx`
**Machine-readable audit:** `reference/WALLET-AUDIT-2026-09-21.csv` (120 unique token rows)

## Bottom line

The 21-09-26 document has **11 tables, 123 data rows, 123 address-like
token occurrences, and 120 unique tokens**. Every token is accounted for.

- **82 valid wallets:** 80 checksum-valid TRON addresses + 2 ETH addresses.
- **5 genuinely new valid TRON addresses** (A1–A5). All five are already in
  `reference/identified-wallets.csv`; none is in the operator master.
- **No valid address in the document is missing from the census.** The first
  pass did not miss a valid wallet.
- **Census remains 92 rows; operator master remains 5 rows.** Quarantine is
  now **28 rows** after adding the document's flagged TAVaw OCR variant.
- **The only table change vs 14-09 is table 11 (A1–A5).** Tables 1–10 are
  identical between the two documents.

## Table-by-table reconciliation

Counts below are per table, before global de-duplication. Three addresses
repeat in tables 1 and 4, so the per-table valid/census totals are three
higher than the global unique totals.

| Table | Data rows | Contents | Census | Quarantine | Notes |
|---|---:|---|---:|---:|---|
| 1 | 30 | 28 valid TRON + 2 TQD UPI strings | 28 | 2 | T1 #2, #3 and #14 repeat in T4 C1, C3 and C11 |
| 2 | 24 | 24 base58-invalid platform-hot strings | 0 | 24 | every string contains TRON-forbidden `0/O/I/l` |
| 3 | 6 | 6 valid TRON operator wallets | 6 | 0 | B1–B5 are in the 5-row master; B6 is census-only |
| 4 | 12 | 11 valid TRON + 1 base58-invalid C12 variant | 11 | 1 | C12 is an OCR variant of the canonical `TEeqmf…D3o…` address |
| 5 | 3 | 1 valid + 2 checksum-invalid desk strings | 3 | 0 | D2/D3 are retained OCR/desk leads; see re-OCR table |
| 6 | 1 | 1 valid TRON exchange wallet | 1 | 0 | E1 |
| 7 | 7 | 4 valid TRON + 2 ETH + 1 partial | 7 | 0 | U1/U2, X1/X2, V1/V2, P1 |
| 8 | 8 | 5 valid + 2 checksum-invalid + 1 FLAG | 7 | 1 | N1–N7 plus the TAVaw variant; FLAG now quarantined |
| 9 | 20 | 20 valid TRON banking-QR wallets | 20 | 0 | Q1–Q20 |
| 10 | 7 | 2 valid + 5 checksum-invalid live-dashboard strings | 7 | 0 | R1–R7; see re-OCR table |
| 11 | 5 | 5 valid TRON new evidence wallets | 5 | 0 | A1–A5, new in 21-09 |
| **Total** | **123** | **80 valid TRON + 2 ETH + 9 checksum-invalid census + 1 FLAG + 24 platform-hot + 1 C12 + 2 UPI + 1 partial** | **92** | **28** | global unique counts |

Per-table valid TRON adds to 83 and per-table census adds to 95 because
`TAhW4QKFYFBZX2qQ8yuZCoVNC4qrVUNJpQ`, `TFpP4TqspHYa1Xzd3L1qkmhyfd6gzEsoQr`
and `TQ9LitUK7iBhayHruHBN7VT9sjtpfarH6v` each appear in both T1 and T4.
Global unique totals are 80 valid TRON and 92 census rows.

## Unique-token classification

| Class | Unique | Census | Quarantine | Operator master | Notes |
|---|---:|---:|---:|---:|---|
| Valid TRON | 80 | 80 | 0 | 5 (subset) | includes A1–A5 and B6; B6 is not in the master |
| ETH | 2 | 2 | 0 | 0 | V1/V2, likely operator-or-user receive wallets |
| Checksum-invalid OCR already in census | 9 | 9 | 0 | 0 | 7 `ai_lead` + 2 desk-support `fact`; cannot exist on-chain |
| Checksum-invalid FLAG | 1 | 0 | 1 | 0 | TAVaw variant, added to quarantine 2026-10-25 |
| Base58-invalid platform-hot | 24 | 0 | 24 | 0 | table 2; all contain `0/O/I/l` |
| Base58-invalid C12 variant | 1 | 0 | 1 | 0 | table 4; canonical `TEeqmf…D3o…` remains in census |
| UPI numeric | 2 | 0 | 2 | 0 | TQD strings are UPI IDs, not wallets |
| Partial | 1 | 1 | 0 | 0 | `TTmepj9RqbfuzTRkuLuQ9vAX4..iEKKeXL`, retained as `partial` |
| **Total** | **120** | **92** | **28** | **5** | |

The 5-row operator master is a subset of the 80 valid TRON addresses; it is
not a separate additional count.

## New wallets added (A1–A5)

All five passed TRON Base58Check validation, are present in the live
`wallet_hits` table, and are present in the GCS mirror (`in_live=true`,
`in_mirror=true`, `le_master=false`).

| ID | Address | Evidence object | Census role | Live hits | Latest hit |
|---|---|---|---|---:|---|
| A1 | `TCLgK89AnXbC9rewvhNb9UgXCc2qJJpBXh` | `deposit/1789365938958.jpg` | `BINANCE_WITHDRAW_10` (exchange withdrawal wallet, not operator) | 1 | 2026-10-05 |
| A2 | `THM4uT2VxzSsBEp8WRbYMzRkiqvbEuBBLU` | `deposit/1789387030596.jpg` | `DEPOSIT_EVIDENCE_UNATTRIBUTED` | 1 | 2026-10-05 |
| A3 | `TFkz4usfWmLHu78zENZU63Znqnmh267B25` | `withdrawal/1789394882913.webp` | `WITHDRAWAL_EVIDENCE_UNATTRIBUTED` | 1 | 2026-10-05 |
| A4 | `TFv1isf5EXpXnu2NPpGSwTgipckNvsmEvz` | `deposit/1789463619457.jpg` | `DEPOSIT_EVIDENCE_UNATTRIBUTED` | 5 | 2026-10-24, `deposit/1792853171957.jpg` |
| A5 | `TCgcGDFVmeqgxXvsUSZAUMkdLcfovc2aVm` | `withdrawal/1789482635019.webp` | `WITHDRAWAL_EVIDENCE_UNATTRIBUTED` | 1 | 2026-10-05 |

A1 is explicitly labelled by the public explorer as a Binance withdrawal
wallet. A2–A5 require transaction-level corroboration for attribution and
case role. None of the five is an operator attribution.

## Cross-table duplicates

Only three addresses occur more than once:

| Address | T1 row | T4 row | Note |
|---|---|---|---|
| `TAhW4QKFYFBZX2qQ8yuZCoVNC4qrVUNJpQ` | #2 | C1 | hawala collection + withdrawal beneficiary |
| `TFpP4TqspHYa1Xzd3L1qkmhyfd6gzEsoQr` | #3 | C3 | crypto-to-fiat bridge + withdrawal beneficiary |
| `TQ9LitUK7iBhayHruHBN7VT9sjtpfarH6v` | #14 | C11 | Andhra collection + withdrawal beneficiary |

The other 117 tokens occur once. That gives 123 occurrences → 120 unique
tokens.

## 14-09 vs 21-09

- Tables 1–10 are identical in both documents.
- The 21-09 document adds table 11 with exactly five checksum-valid TRON
  addresses (A1–A5). These are the only valid additions.
- The 14-09 paragraphs contained the excluded fragment
  `3yHqrRCkN9hYcqXkdDc6ywXELHpci` (a BITCOIN-labelled UPI sign= fragment,
  not a TRON wallet) and the TAVaw flag paragraph. The 21-09 document removed
  those paragraphs. The `3yHqr…` fragment is absent from 21-09 and remains
  excluded; the TAVaw variant is still present in table 8 and is now
  quarantined.
- No existing address changed in tables 1–10.

## Checksum-invalid OCR rows already in the census

These nine strings fail TRON Base58Check, so they cannot exist on-chain.
They were already in the census before this review and are retained as OCR
leads, not operator wallets. They were not new in 21-09 and were not promoted.

| Document row | Address | Census category | Live hits | Re-OCR note |
|---|---|---|---:|---|
| T8 N2 | `TCgLo2vsT4BNxXB5ZNKvW9vev9t54Tv4kt` | `dashboard_ocr` (`ai_lead`) | 39 | no ≤2-character valid neighbour in current census/live; re-read image |
| T8 N7 | `TX1HkKxnsHNPZHdhUETZXJp6NBozdVN9kq` | `dashboard_ocr` (`ai_lead`) | 15 | valid case-variant `TX1HkKxnsHNPzHdhUETZXJp6NBozdVN9kq` has 1 live hit from a different withdrawal key; do not merge without corroboration |
| T10 R1 | `TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWys` | `live_dashboard` (`ai_lead`) | 18 | valid case-variant `TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWyS` has 1 live hit from the same `deposit/1787289222499.png`; re-OCR candidate, do not auto-promote |
| T10 R4 | `TXGFq9BxneuNGUSd4uEgEg6KJ8rJGFaGoD` | `live_dashboard` (`ai_lead`) | 1 | no ≤2-character valid neighbour; re-read image |
| T10 R5 | `TFaoUrWJWmfNJVVeDYTFK423i858yQy3mg` | `live_dashboard` (`ai_lead`) | 2 | no ≤2-character valid neighbour; re-read image |
| T10 R6 | `TFZANTUBkFqYuka4d931vpoa7T5gYu4rVF` | `live_dashboard` (`ai_lead`) | 4 | no ≤2-character valid neighbour; re-read image |
| T10 R7 | `TDqSquXBgUCLYVYC4XZerprLK589dkhSCf` | `live_dashboard` (`ai_lead`) | 1 | no ≤2-character valid neighbour; re-read image |
| T5 D2 | `TJPVHGb3FUPd3754hg8h9vjHoUgZwZCzBb` | `desk_support` (`fact`) | 0 | checksum-invalid despite `fact` status; re-read screenshot before case use |
| T5 D3 | `TJnxipzrpf9NKTTkyWo2YXBJ6HF7otHjar` | `desk_support` (`fact`) | 1 | checksum-invalid despite `fact` status; re-read screenshot before case use |

The two desk-support rows are the only checksum-invalid census rows marked
`fact`. They remain in the census because they have screenshot evidence, but
the address string itself cannot be treated as a verified on-chain address
until it is re-read.

## Quarantine update — TAVaw variant

The 21-09 document's table 8 FLAG is:

`TAVawJ7BF7L9XwSf5RJdtrigHSyVTzzCxf`

It differs from the canonical B6 address by one character:

`TAVawJ7BF7L9XwSf5RJdtr1gHSyVTzzCxf` (`…dtrig…` vs `…dtr1g…`).

The FLAG has the correct base58 alphabet and length but fails TRON
Base58Check. The document itself says it is **not a separate wallet** until
the image is re-verified. It has been added to
`reference/quarantined-wallets.csv` as the 28th row:

- category: `operator_hot_variant`
- source: `chat_file/gggdeepsify_1769499035441_usdtplayer_deposit.jpg`
- quarantined: 2026-10-25
- canonical B6 remains in the census as `operator_hot`, but is **not** in the
  5-row operator master. The FLAG variant has one live hit; canonical B6 has
  zero. Promoting B6 into the master would change the known-operator set and
  needs an explicit case decision, so it was left unchanged.

Quarantine is now 28 rows: 24 platform-hot + 2 UPI + 1 C12 variant + 1 TAVaw
variant. The app does not read `quarantined-wallets.csv`; this change affects
the reference/test artifacts, not the live dashboard. The generator and test
suite now pin 28 rows.

## Operator master intentionally unchanged

`reference/17-CRYPTO-WALLETS.csv` remains the 5-row LE operator/hawala master:

- `TD63G1qvWu7ke2ycW848BjMCj2sHEZZusa`
- `TJ5yq6udNUQ5iQNaapbN5f5YMUvRJKP17B`
- `TQbXYxYw3NtKLaHbKEWtu1xMfPbdD3A5Ef`
- `TUeZqJPJX6tMUwyiaJNWe2TAYmAoH46tW2`
- `TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU`

The following are deliberately **not** in the master:

- A1–A5: evidence-only additions; A1 is an exchange withdrawal wallet and
  A2–A5 need transaction corroboration.
- Canonical B6: valid and in the census as `operator_hot`, but the 21-09
  document does not establish it as part of the LE master attribution set.
- The nine checksum-invalid OCR rows: not valid on-chain addresses.
- All quarantine rows.

## Live verification

- `/api/wallets/reference` → `identified_total=92`, `items=92`,
  `live_decoded=78`; all five new rows show `in_live=true`,
  `in_mirror=true`, `le_master=false`.
- `/api/export/wallets?kind=reference` → 93 CSV lines (header + 92 rows);
  each of the five new addresses appears exactly once.
- `/api/wallets?distinct=1&q=<address>` → each new address appears with
  `known_operator=false`.
- `/api/mirror-sync` → `LIVE — mirror syncing continuously`, healthy, no
  pending tombstones.
- Ports 80/81/82 `/api/healthz` → HTTP 200.

At audit time, the live `wallet_hits` table had **332 distinct addresses**:
178 checksum-valid, 140 checksum-invalid, and 14 not-34T. Of the 178 valid
addresses, 69 are in the document/census and 109 are live-only (not in the
docx, census, or quarantine). Those 109 are visible in the live feed but were
**not** auto-added to the curated census: they lack document-level attribution
and many are OCR variants. Eleven checksum-valid document addresses currently
have zero live hits; they remain evidence-backed census rows, not failures.

## Files changed

- `reference/identified-wallets.csv` — 92 rows; unchanged by this deeper
  review (the five new rows were already added).
- `reference/quarantined-wallets.csv` — 28 rows; TAVaw variant added.
- `reference/WALLET-AUDIT-2026-09-21.csv` — 120 unique token rows with docx
  references, checksum result, census/quarantine/master status, live hits and
  disposition.
- `scripts/build-identified-wallets.py` — quarantine generator now includes
  the TAVaw variant and pins 28 rows.
- `backend/test_wallet_sources.py` — 28-row quarantine pin and
  `test_tavaw_flag_quarantined_not_census`.
- `backend/evidence.py` — census docstring updated to 28 quarantined rows.

## Verification

```bash
python3 -m pytest backend -q
# 240 passed
```

The audit CSV and this note are reference artifacts; the live app continues to
use the 92-row `identified-wallets.csv` already present in the running
container and image.
