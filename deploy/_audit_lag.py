import sqlite3
from datetime import datetime, timezone

c = sqlite3.connect("/root/.local/share/s3-live-monitor/manifest.db")
c.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
print("now_utc", now)

def q(sql, args=()):
    return c.execute(sql, args).fetchone()

print("obj_deposit_max_lm", q("SELECT MAX(last_modified) m, MAX(seen_utc) s, MAX(enriched_utc) e FROM objects WHERE key LIKE 'deposit/%'"))
print("obj_wd_max_lm", q("SELECT MAX(last_modified) m, MAX(seen_utc) s, MAX(enriched_utc) e FROM objects WHERE key LIKE 'withdrawal/%'"))
print("pay_dep_max_lm", q("SELECT MAX(last_modified) m FROM payment_transactions WHERE direction='deposit' AND source_key NOT LIKE 'reference:%'"))
print("pay_wd_max_lm", q("SELECT MAX(last_modified) m FROM payment_transactions WHERE direction='withdrawal' AND source_key NOT LIKE 'reference:%'"))
print("deposit_pending", q("SELECT COUNT(*) n FROM objects WHERE key LIKE 'deposit/%' AND (extract_json IS NULL OR extract_json='') AND enriched=0"))
print("deposit_enriched_null_amt", q("SELECT COUNT(*) n FROM objects WHERE key LIKE 'deposit/%' AND json_extract(extract_json,'$.kind')='deposit' AND json_extract(extract_json,'$.amount_inr') IS NULL"))
print("last_hour_objects_deposit", q("SELECT COUNT(*) n FROM objects WHERE key LIKE 'deposit/%' AND last_modified>=?", (now[:11] + "00:00:00Z",)))

cutoff = now  # placeholder replaced below
from datetime import timedelta
cut = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
print("cutoff", cut)
print("pay_dep_24h", q(
    "SELECT COUNT(DISTINCT source_key) n, ROUND(SUM(amount_inr)) s, MIN(last_modified) a, MAX(last_modified) b FROM payment_transactions WHERE direction='deposit' AND source_key NOT LIKE 'reference:%' AND last_modified>=?",
    (cut,),
))
print("obj_dep_24h", q(
    "SELECT COUNT(*) n, MIN(last_modified) a, MAX(last_modified) b FROM objects WHERE key LIKE 'deposit/%' AND last_modified>=?",
    (cut,),
))
print("obj_dep_24h_pending", q(
    "SELECT COUNT(*) n FROM objects WHERE key LIKE 'deposit/%' AND last_modified>=? AND (extract_json IS NULL OR extract_json='')",
    (cut,),
))
# lag minutes
for label, row in [
    ("obj_dep", q("SELECT MAX(last_modified) m FROM objects WHERE key LIKE 'deposit/%'")),
    ("pay_dep", q("SELECT MAX(last_modified) m FROM payment_transactions WHERE direction='deposit' AND source_key NOT LIKE 'reference:%'")),
    ("obj_seen_dep", q("SELECT MAX(seen_utc) m FROM objects WHERE key LIKE 'deposit/%'")),
]:
    m = row["m"] if row else None
    if not m:
        print(label, "none")
        continue
    dt = datetime.fromisoformat(m.replace("Z", "+00:00"))
    lag_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    print(f"{label} newest={m} lag_min={lag_min:.1f}")
