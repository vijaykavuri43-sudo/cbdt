#!/usr/bin/env python3
"""Audit OCR amounts for the officer PhonePe slip and rupee-eaten pattern."""
import json
import sqlite3

DB = "/root/.local/share/s3-live-monitor/manifest.db"
KEY = "deposit/1789721443450.jpeg"

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

row = c.execute(
    "SELECT key, extract_json FROM objects WHERE key=?", (KEY,)
).fetchone()
print("=== exhibit ===")
if not row:
    alt = c.execute(
        "SELECT key FROM objects WHERE key LIKE '%1789721443450%'"
    ).fetchall()
    print("missing key; like=", [r["key"] for r in alt[:10]])
else:
    p = json.loads(row["extract_json"] or "{}")
    print(
        json.dumps(
            {
                "key": row["key"],
                "amount_inr": p.get("amount_inr"),
                "amount_value": p.get("amount_value"),
                "utr": p.get("utr"),
                "vpa": p.get("vpa"),
                "kind": p.get("kind"),
                "engine": p.get("engine"),
                "confidence": p.get("confidence"),
                "twin_artifact": p.get("twin_artifact"),
                "glyph_collision": p.get("glyph_collision"),
                "text_sample": p.get("text_sample"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

print("=== payment row ===")
for r in c.execute(
    "SELECT source_key, amount_inr, utr, rail_id FROM payment_transactions WHERE source_key=?",
    (KEY,),
):
    print(dict(r))

print("=== rupee-eaten suspects (amount 21k/23k/25k/27k/71k/73k/75k) ===")
q = """
SELECT amount_inr, COUNT(*) n
FROM payment_transactions
WHERE direction IN ('deposit','withdrawal')
  AND amount_inr IN (21000,23000,25000,27000,71000,73000,75000,212000,232000)
GROUP BY amount_inr ORDER BY n DESC
"""
for r in c.execute(q):
    print(dict(r))

print("=== sample 23000 rows ===")
for r in c.execute(
    """
    SELECT p.source_key, p.amount_inr, p.utr, p.rail_id,
           json_extract(o.extract_json,'$.text_sample') sample
    FROM payment_transactions p
    JOIN objects o ON o.key=p.source_key
    WHERE p.amount_inr=23000 AND p.direction='deposit'
    LIMIT 8
    """
):
    d = dict(r)
    d["sample"] = (d.get("sample") or "")[:220]
    print(json.dumps(d, ensure_ascii=False))

print("=== missing amounts deposit/withdrawal ===")
print(
    dict(
        c.execute(
            """
            SELECT
              SUM(CASE WHEN json_extract(extract_json,'$.kind')='deposit' THEN 1 ELSE 0 END) deposits,
              SUM(CASE WHEN json_extract(extract_json,'$.kind')='deposit'
                       AND json_extract(extract_json,'$.amount_inr') IS NULL THEN 1 ELSE 0 END) dep_no_amt,
              SUM(CASE WHEN json_extract(extract_json,'$.kind')='withdrawal' THEN 1 ELSE 0 END) withdrawals,
              SUM(CASE WHEN json_extract(extract_json,'$.kind')='withdrawal'
                       AND json_extract(extract_json,'$.amount_inr') IS NULL THEN 1 ELSE 0 END) wd_no_amt
            FROM objects WHERE extract_json IS NOT NULL
            """
        ).fetchone()
    )
)
