import json
import sqlite3
from datetime import datetime, timedelta, timezone

c = sqlite3.connect("/root/.local/share/s3-live-monitor/manifest.db")
c.row_factory = sqlite3.Row
cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
print("CUTOFF", cutoff)
vpas = [
    "8959856301@okbizaxis",
    "7903709938-1@okbizaxis",
    "8053100641@okbizaxis",
    "9588564069@mairtel",
    "9904324944@okbizaxis",
    "4769@okbizaxis",
    "paytm.s23zag4@pty",
    "goay-122002240@okbizaxis",
]
print("=== per-VPA amount histogram (24h deposits) ===")
for vpa in vpas:
    rows = c.execute(
        """SELECT source_key, amount_inr, utr, last_modified, rail_role, role_confidence
           FROM payment_transactions
           WHERE direction='deposit' AND rail_id=? AND last_modified>=?
             AND source_key NOT LIKE 'reference:%'
           ORDER BY amount_inr DESC""",
        (vpa, cutoff),
    ).fetchall()
    amts = [r["amount_inr"] or 0 for r in rows]
    n = len(amts)
    s = sum(amts)
    uniq = len({r["source_key"] for r in rows})
    over1l = sum(1 for a in amts if a >= 100000)
    over5l = sum(1 for a in amts if a >= 500000)
    over10l = sum(1 for a in amts if a >= 1000000)
    median = sorted(amts)[n // 2] if n else 0
    mean = (s / n) if n else 0
    print(
        f"\n{vpa} rows={n} distinct_slips={uniq} sum={s:.0f} mean={mean:.0f} "
        f"median={median:.0f} >=1L={over1l} >=5L={over5l} >=10L={over10l}"
    )
    print("  top amounts:", [round(a) for a in amts[:8]])

print("\n=== 8959856301@okbizaxis every 24h slip + OCR sample ===")
rows = c.execute(
    """SELECT p.source_key, p.amount_inr, p.utr, p.last_modified, p.rail_role, p.role_confidence,
              json_extract(o.extract_json,'$.engine') engine,
              json_extract(o.extract_json,'$.confidence') conf,
              json_extract(o.extract_json,'$.amount_tesseract_raw') raw,
              json_extract(o.extract_json,'$.amount_rejected_inr') rejected,
              json_extract(o.extract_json,'$.twin_artifact') twin,
              json_extract(o.extract_json,'$.malformed_amount') malformed,
              substr(json_extract(o.extract_json,'$.text_sample'),1,320) sample
       FROM payment_transactions p
       LEFT JOIN objects o ON o.key=p.source_key
       WHERE p.direction='deposit' AND p.rail_id='8959856301@okbizaxis' AND p.last_modified>=?
         AND p.source_key NOT LIKE 'reference:%'
       ORDER BY p.amount_inr DESC""",
    (cutoff,),
).fetchall()
out = []
for r in rows:
    rec = {k: r[k] for k in r.keys()}
    out.append(rec)
    print(
        "---", rec["source_key"], "amt", rec["amount_inr"],
        "engine", rec["engine"], "conf", rec["conf"],
        "role", rec["rail_role"], rec["role_confidence"],
    )
    print("   raw", rec["raw"], "rejected", rec["rejected"], "twin", rec["twin"], "malformed", rec["malformed"])
    print("   sample:", (rec["sample"] or "").replace("\n", " ")[:260])

print("\n=== duplicate source_key on top VPA ===")
dups = c.execute(
    """SELECT source_key, COUNT(*) n, SUM(amount_inr) s
       FROM payment_transactions
       WHERE direction='deposit' AND rail_id='8959856301@okbizaxis' AND last_modified>=?
       GROUP BY source_key HAVING n>1""",
    (cutoff,),
).fetchall()
print("dups", [dict(r) for r in dups])

print("\n=== SUM vs per-slip MAX for top 3 ===")
for vpa in vpas[:3]:
    naive = c.execute(
        "SELECT COUNT(*) n, SUM(amount_inr) s FROM payment_transactions "
        "WHERE direction='deposit' AND rail_id=? AND last_modified>=? AND source_key NOT LIKE 'reference:%'",
        (vpa, cutoff),
    ).fetchone()
    dedup = c.execute(
        """SELECT COUNT(*) n, SUM(amt) s FROM (
             SELECT source_key, MAX(amount_inr) amt FROM payment_transactions
             WHERE direction='deposit' AND rail_id=? AND last_modified>=? AND source_key NOT LIKE 'reference:%'
             GROUP BY source_key)""",
        (vpa, cutoff),
    ).fetchone()
    print(vpa, "naive", dict(naive), "per_slip_max", dict(dedup))

# How many 24h deposit rows are tesseract >= 1 lakh
print("\n=== 24h deposit slips amount>=1L by engine ===")
print(dict(c.execute(
    """SELECT COALESCE(json_extract(o.extract_json,'$.engine'),'?') engine, COUNT(*) n, SUM(p.amount_inr) s
       FROM payment_transactions p LEFT JOIN objects o ON o.key=p.source_key
       WHERE p.direction='deposit' AND p.last_modified>=? AND p.amount_inr>=100000
         AND p.source_key NOT LIKE 'reference:%'
       GROUP BY 1""",
    (cutoff,),
).fetchall() and True))
for r in c.execute(
    """SELECT COALESCE(json_extract(o.extract_json,'$.engine'),'?') engine, COUNT(*) n, ROUND(SUM(p.amount_inr)) s
       FROM payment_transactions p LEFT JOIN objects o ON o.key=p.source_key
       WHERE p.direction='deposit' AND p.last_modified>=? AND p.amount_inr>=100000
         AND p.source_key NOT LIKE 'reference:%'
       GROUP BY 1""",
    (cutoff,),
):
    print(dict(r))

open("/tmp/_vpa895.json", "w").write(json.dumps(out, indent=2, default=str))
print("wrote /tmp/_vpa895.json", len(out))
