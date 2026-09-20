#!/usr/bin/env python3
import json
import sqlite3

DB = "/root/.local/share/s3-live-monitor/manifest.db"
KEY = "deposit/1789721443450.jpeg"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
p = json.loads(
    c.execute("SELECT extract_json FROM objects WHERE key=?", (KEY,)).fetchone()[0]
)
pay = c.execute(
    "SELECT amount_inr, utr, rail_id FROM payment_transactions WHERE source_key=?",
    (KEY,),
).fetchone()
print(
    json.dumps(
        {
            "amount_inr": p.get("amount_inr"),
            "raw": p.get("amount_tesseract_raw"),
            "rupee_eaten": p.get("rupee_eaten"),
            "utr_extract": p.get("utr"),
            "pay": dict(pay) if pay else None,
        },
        ensure_ascii=False,
    )
)
print("suspects after")
for r in c.execute(
    """
    SELECT amount_inr, COUNT(*) n FROM payment_transactions
    WHERE direction IN ('deposit','withdrawal')
      AND amount_inr IN (21000,23000,25000,27000,71000,73000,75000)
    GROUP BY amount_inr ORDER BY n DESC
    """
):
    print(dict(r))
print(
    dict(
        c.execute(
            """
            SELECT
              SUM(CASE WHEN json_extract(extract_json,'$.kind')='deposit'
                       AND json_extract(extract_json,'$.amount_inr') IS NULL THEN 1 ELSE 0 END) dep_no_amt,
              SUM(CASE WHEN json_extract(extract_json,'$.kind')='withdrawal'
                       AND json_extract(extract_json,'$.amount_inr') IS NULL THEN 1 ELSE 0 END) wd_no_amt
            FROM objects WHERE extract_json IS NOT NULL
            """
        ).fetchone()
    )
)
