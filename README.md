# cricketbetting — S3/GCS Mule Money-Flow Monitor

Local investigation dashboard for **read-only monitoring of the publicly exposed bucket**
`bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com` (VULN-004).

> Authorization: `STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING` — see repo root `AUTHORIZATION.md`.
> The app issues **GET/List only**. It never writes to or deletes anything in the bucket.

## What it does

| Tab | Function |
|---|---|
| **Overview** | Sweep/poll status, objects indexed, 14-day upload histogram, per-prefix inventory, deposit/withdrawal OCR totals |
| **Bucket Explorer** | Folder tree over the full manifest (SQLite), search, sort, image/audio/text preview, multi-select download with sha256 |
| **Live Feed** | SSE stream — every new object appears as a card (screenshot thumbnails, wav players, decoded QR badges) with optional sound alert. This is the "catch them live" screen |
| **Mule Money Flows** | Role-aware deposit and withdrawal evidence: gambler → receiving rail and sending rail → gambler, consolidated candidate ranking, confidence and exact provenance |
| **UPI / Mules** | VPAs decoded from payment QRs, cross-checked against the case mule master list; NEW VPAs highlighted |
| **Evidence & Downloads** | Bulk download jobs → files land in `intelligence/analysis/CASE-2026-CBTF/live-monitor/<UTC-date>/` with `_MANIFEST.json`, sha256 chain-of-custody appended to `intelligence/audit/collection-log.jsonl` |

## Run

```bash
cd s3-live-monitor
./start-dev.sh          # backend :8787 + frontend :5173
```

Open **http://localhost:5173** — the console is **gated**: paste the access key stored at
`~/.local/share/s3-live-monitor/access-token.txt`
or set `S3LM_TOKEN=…` before starting. Every `/api` route rejects requests without it
(401), including image/audio previews and the SSE stream.

- First boot performs a full manifest sweep (~1M keys — runs in background, UI usable immediately).
- Later boots do a fast catch-up poll instead; root-level files and brand-new top-level folders
  are discovered automatically every cold cycle.
- Manifest DB: `~/.local/share/s3-live-monitor/manifest.db` (fast local disk; delete to re-index).
- Stop: `./stop-dev.sh`
- **Gate off-switch:** `S3LM_NO_GATE=1 ./start-dev.sh` skips the access-key screen.
  The bucket needs NO credentials either way (it is public) — the key protects only this
  console because it concentrates victim PII and case evidence on one page.

## Seeing everything in the bucket

| Where | What |
|---|---|
| **Overview → per-prefix table** | full inventory: counts + bytes + newest upload per folder |
| **Bucket Explorer** | click through any folder; `Recursive ON` lists all descendants at once; type chips (Images/Audio/Video/Docs/APK) and time chips (24h/7d/30d); select-all-page or download whole folder view (≤5000/job, sha256 logged) |
| **Search All Data** | substring search across the entire manifest with extension / date / size filters |
| **POST `/api/verify`** | cold catch-up of every prefix + root files + live folder re-discovery — reports anything hot-polling missed |

Root-level loose objects (`mongod.log`, rotated gz logs, `nginx_last_hour.log`, …) are indexed
under the `(root)` pseudo-folder and visible in Explorer at root.

## Backend env knobs

| Var | Default | Purpose |
|---|---|---|
| `S3LM_POLL_SEC` | 10 | hot-prefix watermark poll interval |
| `S3LM_COLD_SEC` | 600 in Compose | bounded version-aware fallback crawl interval |
| `S3LM_CRAWL_CAP` | 5000 in Compose | maximum objects visited per prefix/cold cycle |
| `S3LM_SWEEP_WORKERS` | 6 | parallel prefix sweeps |
| `S3LM_TOKEN` | empty | require bearer token on all API routes |
| `S3LM_HOST` / `S3LM_PORT` | 127.0.0.1:8787 | bind address (keep localhost — dashboard exposes PII) |

## Known limitations

- OCR amounts can misparse (e.g. long UTR digits read as amount) — treat per-row values as
  `ai_lead` confidence, verify against slip image before case use.
- WAV voice-note transcription is not wired into this dashboard yet (existing whisper
  workflow in `intelligence/analysis/CASE-2026-CBTF/chat-deep-dive-2026-08-22/` covers it).
- Enrichment backlog after first sweep is large (~700K images); it processes newest-first,
  so live monitoring works from minute one while history catches up.

## Accuracy model

The Overview deliberately reports three populations separately: current S3 inventory,
preserved GCS mirror, and locally indexed/decoded evidence. The sync daemon notifies the
manifest immediately after every successful GCS upload; a bounded, persistent,
version-aware crawl is the fallback for missed notifications and same-key replacements.
See [`MONITORING-IMPROVEMENT-PLAN.md`](MONITORING-IMPROVEMENT-PLAN.md) for acceptance
criteria, IAM, VM sizing and deployment checks.

For payment intelligence, receipt identifiers are classified as sender,
beneficiary or unknown from nearby labels and QR protocol semantics. Deposits
only promote beneficiary rails; withdrawals only promote sender rails. Explicit
opposite-side identifiers are excluded, while ambiguous legacy evidence remains
visible with low role confidence. Observed transfers are `fact`; the suspected
mule score/priority is an `analyst_conclusion` that requires corroboration.

## Layout

```
backend/   FastAPI app (config, db, s3client, collector, enrich, evidence)
frontend/  Vite React TS app (pages: Overview, Explorer, LiveFeed, Deposits, Upis, Evidence)
```
