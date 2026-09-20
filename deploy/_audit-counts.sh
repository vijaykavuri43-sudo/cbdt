#!/bin/bash
set -euo pipefail
sudo docker exec s3lm cat /root/.local/share/s3-live-monitor/access-token.txt
echo
sudo docker exec s3lm python3 - <<'PY'
import sqlite3
c=sqlite3.connect("/data/s3-live-monitor/manifest.db")
c.row_factory=sqlite3.Row
def q(sql):
    return c.execute(sql).fetchone()[0]
print("deposit_objects", q("select count(*) from objects where key like 'deposit/%'"))
print("deposit_kind", q("select count(*) from objects where json_extract(extract_json,'$.kind')='deposit'"))
print("deposit_enriched", q("select count(*) from objects where key like 'deposit/%' and extract_json is not null"))
print("wd_objects", q("select count(*) from objects where key like 'withdrawal/%'"))
print("wd_kind", q("select count(*) from objects where json_extract(extract_json,'$.kind')='withdrawal'"))
print("pt_upi", q("select count(*) from payment_transactions where rail_type='upi'"))
print("pt_bank", q("select count(*) from payment_transactions where rail_type='bank_account'"))
print("pt_crypto", q("select count(*) from payment_transactions where rail_type='crypto_wallet'"))
print("upi_no_at", c.execute("select rail_id from payment_transactions where rail_type='upi' and rail_id!='' and rail_id not like '%@%' limit 8").fetchall())
print("bank_has_at", c.execute("select rail_id from payment_transactions where rail_type='bank_account' and rail_id like '%@%' limit 8").fetchall())
print("prefix_deposit", c.execute("select n from prefix_stats_cache where p='deposit/'").fetchone())
PY
