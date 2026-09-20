import sqlite3
c = sqlite3.connect("/data/s3-live-monitor/manifest.db")
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
print("upi_no_at", c.execute("select count(*) from payment_transactions where rail_type='upi' and rail_id!='' and rail_id not like '%@%'").fetchone()[0])
print("bank_has_at", c.execute("select count(*) from payment_transactions where rail_type='bank_account' and rail_id like '%@%'").fetchone()[0])
print("upi_no_at_samples", [r[0] for r in c.execute("select rail_id from payment_transactions where rail_type='upi' and rail_id!='' and rail_id not like '%@%' limit 8")])
print("bank_samples", [r[0] for r in c.execute("select rail_id from payment_transactions where rail_type='bank_account' and rail_id!='' limit 8")])
