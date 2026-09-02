#!/usr/bin/env python3
"""Verify exhaustive deposit/withdrawal processing — no missing slips."""

import sqlite3, json, time, pathlib, re, os, sys
from datetime import datetime, timezone

# Use data/manifest.db directly, not HOME default
DB_CANDIDATES = [
    pathlib.Path(__file__).resolve().parents[1] / "data" / "manifest.db",
    pathlib.Path.home() / ".local" / "share" / "s3-live-monitor" / "manifest.db",
]
db_path = None
for p in DB_CANDIDATES:
    if p.exists() and p.stat().st_size > 1024 * 100:  # prefer the 423M one
        db_path = p
        break
if not db_path:
    db_path = DB_CANDIDATES[1]
print(f"[verify] DB_PATH={db_path} size={db_path.stat().st_size / 1024 / 1024:.1f} MB")

# Connect pure sqlite, no backend writer_lock
conn = sqlite3.connect(str(db_path), timeout=30)
conn.row_factory = sqlite3.Row


def q1(sql, args=()):
    return conn.execute(sql, args).fetchone()


def qall(sql, args=()):
    return conn.execute(sql, args).fetchall()


total = q1("SELECT COUNT(*) c FROM objects")["c"]
print(f"total objects: {total}")

# Per prefix stats
rows = qall(
    "SELECT top_prefix p, COUNT(*) n FROM objects GROUP BY top_prefix ORDER BY n DESC"
)
for r in rows[:15]:
    print(f"  {r['p']!r}: {r['n']}")

# Deposit/withdrawal breakdown with enrichment
for pref in [
    "deposit/",
    "withdrawal/",
    "bankingqr/",
    "qrcode/",
    "paymentgateway/",
    "payment/",
    "chat_file/",
]:
    r = q1("SELECT COUNT(*) c FROM objects WHERE top_prefix=?", (pref,))
    e = q1("SELECT COUNT(*) c FROM objects WHERE top_prefix=? AND enriched=1", (pref,))
    payload = q1(
        "SELECT COUNT(*) c FROM objects WHERE top_prefix=? AND extract_json IS NOT NULL",
        (pref,),
    )
    pending = q1(
        "SELECT COUNT(*) c FROM objects WHERE top_prefix=? AND enriched=0 AND extract_json IS NULL",
        (pref,),
    )
    print(
        f"{pref:16} total={r['c']:6} enriched={e['c']:5} payload={payload['c']:5} pending_raw={pending['c']:6}"
    )

# Pending with _IMG_CLAUSE (exhaustive image types)
PENDING_WHERE_SQL = """
SELECT COUNT(*) c FROM objects WHERE
 (enriched=0 AND extract_json IS NULL)
 AND (enrich_retry_utc IS NULL OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now'))
 AND top_prefix IN ('deposit/','withdrawal/','bankingqr/','qrcode/','payment/','paymentgateway/')
 AND (ext IN ('.jpg','.jpeg','.png','.webp','.gif','.bmp','.tiff','.tif','.heic','.heif','.avif','.jfif')
      OR (ext='' AND (lower(key) LIKE '%.jpg' OR lower(key) LIKE '%.jpeg' OR lower(key) LIKE '%.png' OR lower(key) LIKE '%.webp' OR lower(key) LIKE '%.gif' OR lower(key) LIKE '%.bmp' OR lower(key) LIKE '%.tiff' OR lower(key) LIKE '%.tif' OR lower(key) LIKE '%.heic' OR lower(key) LIKE '%.heif' OR lower(key) LIKE '%.avif' OR lower(key) LIKE '%.jfif')))
"""
pending_img = q1(PENDING_WHERE_SQL)["c"]
print(
    f"pending enriched queue (QR+deposit/withdrawal+payment, image only, exhaustive): {pending_img}"
)

# Deposit/withdrawal pending image only
for pref in ["deposit/", "withdrawal/"]:
    cnt = q1(
        f"SELECT COUNT(*) c FROM objects WHERE top_prefix=? AND enriched=0 AND extract_json IS NULL AND (ext IN ('.jpg','.jpeg','.png','.webp','.gif','.bmp','.tiff','.tif','.heic','.heif','.avif','.jfif') OR (ext='' AND (lower(key) LIKE '%.jpg' OR lower(key) LIKE '%.jpeg' OR lower(key) LIKE '%.png' OR lower(key) LIKE '%.webp')))",
        (pref,),
    )["c"]
    print(f"  pending image {pref}: {cnt}")

# Enrichment progress
total_r = q1(
    "SELECT COUNT(*) total, SUM(enriched) done, SUM(CASE WHEN extract_json IS NOT NULL THEN 1 ELSE 0 END) with_payload FROM objects"
)
print(
    f"enrichment_progress: total={total_r['total']} done={total_r['done'] or 0} with_payload={total_r['with_payload'] or 0} pending_img={pending_img}"
)

# Payment transactions
try:
    pt_total = q1("SELECT COUNT(*) c FROM payment_transactions")["c"]
    print(f"payment_transactions: {pt_total}")
    for d in ["deposit", "withdrawal"]:
        c = q1("SELECT COUNT(*) c FROM payment_transactions WHERE direction=?", (d,))[
            "c"
        ]
        # distinct rails
        rails = q1(
            "SELECT COUNT(DISTINCT rail_type||':'||rail_id) c FROM payment_transactions WHERE direction=? AND rail_id!=''",
            (d,),
        )["c"]
        # sum amount (sanitized)
        s = q1(
            "SELECT COALESCE(SUM(amount_inr),0) s FROM (SELECT source_key, MAX(amount_inr) amount_inr FROM payment_transactions WHERE direction=? GROUP BY source_key)",
            (d,),
        )["s"]
        print(f"  {d}: rows={c} distinct_rails={rails} sum_amount={s}")
    # top rails
    top = qall(
        "SELECT direction, rail_type, rail_id, COUNT(*) n FROM payment_transactions WHERE rail_id!='' GROUP BY direction, rail_type, rail_id ORDER BY n DESC LIMIT 5"
    )
    print("top rails:")
    for r in top:
        print(f"  {r['direction']} {r['rail_type']} {r['rail_id']} n={r['n']}")
except Exception as e:
    print(f"payment_transactions error: {e}")

# Kind breakdown
rows = qall(
    "SELECT json_extract(extract_json,'$.kind') k, COUNT(*) n FROM objects WHERE extract_json IS NOT NULL GROUP BY k"
)
print("kinds:")
for r in rows:
    print(f"  {r['k']!r}: {r['n']}")

# Amount coverage for deposits
rows = qall(
    "SELECT json_extract(extract_json,'$.amount_inr') amt, COUNT(*) n FROM objects WHERE json_extract(extract_json,'$.kind')='deposit' GROUP BY amt IS NOT NULL"
)
print(f"deposit amount coverage: rows with amount? {rows}")
# Check null amounts
null_cnt = q1(
    "SELECT COUNT(*) c FROM objects WHERE json_extract(extract_json,'$.kind')='deposit' AND json_extract(extract_json,'$.amount_inr') IS NULL"
)["c"]
with_amt = q1(
    "SELECT COUNT(*) c FROM objects WHERE json_extract(extract_json,'$.kind')='deposit' AND json_extract(extract_json,'$.amount_inr') IS NOT NULL"
)["c"]
print(f"  deposit null amount: {null_cnt} with amount: {with_amt}")

# Ext backfill status
empty_ext = q1("SELECT COUNT(*) c FROM objects WHERE ext=''")["c"]
print(
    f"ext='' rows (need backfill): {empty_ext} / {total} ({empty_ext / total * 100:.1f}%)"
)

# Sample deposit payloads with text_sample and amount
print("\n--- sample deposit extracts (5) ---")
for r in qall(
    "SELECT key, extract_json FROM objects WHERE top_prefix='deposit/' AND extract_json IS NOT NULL LIMIT 5"
):
    j = json.loads(r["extract_json"])
    print(
        f"{r['key']}: kind={j.get('kind')} amount={j.get('amount_inr')} vpa={j.get('vpa')} utr={j.get('utr')} conf={j.get('confidence')}"
    )
    print(f"  text_sample: {j.get('text_sample', '')[:120]!r}")

# Verify no missing: compare GCS vs DB? If object_store available, list counts? Try to do live check if credentials present
print("\n--- live GCS reachability (if credentials present) ---")
try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    os.environ.setdefault(
        "S3LM_DATA", str(pathlib.Path(__file__).resolve().parents[1] / "data")
    )
    from backend import config
    from backend.gcsclient import GCSMirrorClient

    # only try if google creds exist
    if (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or pathlib.Path.home()
        .joinpath(".config/gcloud/application_default_credentials.json")
        .exists()
    ):
        gc = GCSMirrorClient(config.GCS_BUCKET, config.GCS_PREFIX)
        ok = gc.head_bucket_ok()
        print(f"GCS head ok: {ok} bucket={config.GCS_BUCKET}/{config.GCS_PREFIX}")
        if ok:
            page = gc.list_page("deposit/", max_keys=5)
            print(f"GCS deposit sample 5: {[o['key'] for o in page['objects']][:3]}")
    else:
        print("no GCP creds found, skipping live check")
except Exception as e:
    print(f"live check skipped: {e}")

# Write intelligence case report
out_dir = (
    pathlib.Path(__file__).resolve().parents[2]
    / "intelligence"
    / "analysis"
    / "CASE-2026-CBTF"
    / "live-monitor"
)
out_dir.mkdir(parents=True, exist_ok=True)
report = {
    "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "db_path": str(db_path),
    "total_objects": total,
    "per_prefix": [
        {"p": r["p"], "n": r["n"]}
        for r in qall(
            "SELECT top_prefix p, COUNT(*) n FROM objects GROUP BY top_prefix ORDER BY n DESC"
        )
    ],
    "deposit": {
        "total": q1("SELECT COUNT(*) c FROM objects WHERE top_prefix='deposit/'")["c"],
        "enriched": q1(
            "SELECT COUNT(*) c FROM objects WHERE top_prefix='deposit/' AND enriched=1"
        )["c"],
        "pending": q1(
            "SELECT COUNT(*) c FROM objects WHERE top_prefix='deposit/' AND enriched=0 AND extract_json IS NULL"
        )["c"],
    },
    "withdrawal": {
        "total": q1("SELECT COUNT(*) c FROM objects WHERE top_prefix='withdrawal/'")[
            "c"
        ],
        "enriched": q1(
            "SELECT COUNT(*) c FROM objects WHERE top_prefix='withdrawal/' AND enriched=1"
        )["c"],
        "pending": q1(
            "SELECT COUNT(*) c FROM objects WHERE top_prefix='withdrawal/' AND enriched=0 AND extract_json IS NULL"
        )["c"],
    },
    "pending_img_exhaustive": pending_img,
    "payment_transactions": pt_total if "pt_total" in locals() else 0,
    "enrichment_progress": {
        "total": total_r["total"],
        "done": total_r["done"],
        "with_payload": total_r["with_payload"],
        "pending_img": pending_img,
    },
    "ext_empty": empty_ext,
}
(out_dir / "exhaustive-verification.json").write_text(json.dumps(report, indent=2))
print(f"\n[wrote] {out_dir / 'exhaustive-verification.json'}")
