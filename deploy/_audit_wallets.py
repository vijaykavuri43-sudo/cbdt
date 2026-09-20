#!/usr/bin/env python3
"""Live wallet vs date-range unique counts. No secrets."""
import os
import sqlite3

paths = [
    "/root/.local/share/s3-live-monitor/manifest.db",
    os.environ.get("S3LM_DATA", "") + "/manifest.db",
]
paths = [p for p in paths if p and p != "/manifest.db"]
dbp = next((p for p in paths if os.path.isfile(p)), None)
print("db", dbp)
if not dbp:
    raise SystemExit("no db")
c = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

def q(sql, args=()):
    return c.execute(sql, args).fetchone()

def qall(sql, args=()):
    return c.execute(sql, args).fetchall()

print("wallet_hits", dict(q("SELECT COUNT(*) n, COUNT(DISTINCT address) d FROM wallet_hits")))
print("wallet_by_net", [dict(r) for r in qall(
    "SELECT network, COUNT(*) hits, COUNT(DISTINCT address) addrs FROM wallet_hits GROUP BY network"
)])
print("pt_crypto", dict(q(
    "SELECT COUNT(*) n, COUNT(DISTINCT rail_id) d, COUNT(DISTINCT source_key) slips, "
    "COALESCE(SUM(amount_inr),0) inr FROM payment_transactions "
    "WHERE rail_type='crypto_wallet' AND rail_id!='' AND source_key NOT LIKE 'reference:%'"
)))
print("pt_crypto_shape", dict(q(
    "SELECT COUNT(DISTINCT rail_id) d FROM payment_transactions WHERE source_key NOT LIKE 'reference:%' AND rail_id!='' "
    "AND (rail_id LIKE '0x%' OR (substr(rail_id,1,1)='T' AND length(rail_id) BETWEEN 26 AND 40))"
)))
print("overlap_wallet_pt", dict(q(
    "SELECT COUNT(*) n FROM ("
    " SELECT DISTINCT address FROM wallet_hits"
    " INTERSECT"
    " SELECT DISTINCT rail_id FROM payment_transactions WHERE rail_type='crypto_wallet' AND rail_id!=''"
    ")"
)))
print("only_wallet_hits", dict(q(
    "SELECT COUNT(*) n FROM ("
    " SELECT DISTINCT address FROM wallet_hits"
    " EXCEPT"
    " SELECT DISTINCT rail_id FROM payment_transactions WHERE rail_type='crypto_wallet' AND rail_id!=''"
    ")"
)))
print("only_payment_rail", dict(q(
    "SELECT COUNT(*) n FROM ("
    " SELECT DISTINCT rail_id FROM payment_transactions WHERE rail_type='crypto_wallet' AND rail_id!=''"
    " EXCEPT"
    " SELECT DISTINCT address FROM wallet_hits"
    ")"
)))
print("dup_pt_trim", dict(q(
    "SELECT COUNT(*) n FROM ("
    " SELECT TRIM(rail_id) t, COUNT(DISTINCT rail_id) c FROM payment_transactions "
    " WHERE rail_type='crypto_wallet' AND rail_id!='' GROUP BY TRIM(rail_id) HAVING c>1)"
)))
print("objects", dict(q("SELECT COUNT(*) n FROM objects")))
print("wallet_hits_folders", [dict(r) for r in qall(
    "SELECT CASE "
    " WHEN key LIKE 'deposit/%' THEN 'deposit' "
    " WHEN key LIKE 'withdrawal/%' THEN 'withdrawal' "
    " WHEN key LIKE 'bankingqr/%' OR key LIKE 'qrcode/%' THEN 'bankingqr' "
    " ELSE 'other' END folder, COUNT(*) hits, COUNT(DISTINCT address) addrs "
    " FROM wallet_hits GROUP BY 1 ORDER BY hits DESC"
)])
print("pt_crypto_folders", [dict(r) for r in qall(
    "SELECT CASE "
    " WHEN source_key LIKE 'deposit/%' THEN 'deposit' "
    " WHEN source_key LIKE 'withdrawal/%' THEN 'withdrawal' "
    " WHEN source_key LIKE 'bankingqr/%' OR source_key LIKE 'qrcode/%' THEN 'bankingqr' "
    " ELSE 'other' END folder, COUNT(DISTINCT rail_id) addrs, COUNT(DISTINCT source_key) slips "
    " FROM payment_transactions WHERE rail_type='crypto_wallet' AND rail_id!='' "
    " GROUP BY 1 ORDER BY slips DESC"
)])
print("date_window_pt", dict(q(
    "SELECT COUNT(DISTINCT rail_id) d, COUNT(DISTINCT source_key) slips, COALESCE(SUM(amount_inr),0) inr "
    " FROM payment_transactions WHERE rail_type='crypto_wallet' AND rail_id!='' "
    " AND source_key NOT LIKE 'reference:%' "
    " AND last_modified>='2024-01-01T00:00:00Z' AND last_modified<='2026-09-18T23:59:59Z' "
    " AND (rail_id LIKE '0x%' OR (substr(rail_id,1,1)='T' AND length(rail_id) BETWEEN 26 AND 40))"
)))
print("date_window_wh", dict(q(
    "SELECT COUNT(DISTINCT address) d, COUNT(*) hits FROM wallet_hits "
    " WHERE last_modified>='2024-01-01T00:00:00Z' AND last_modified<='2026-09-18T23:59:59Z'"
)))
print("crypto_amount_sample", [dict(r) for r in qall(
    "SELECT rail_id, amount_inr, COUNT(*) n FROM payment_transactions "
    " WHERE rail_type='crypto_wallet' AND amount_inr IS NOT NULL AND amount_inr>0 "
    " GROUP BY rail_id, amount_inr ORDER BY n DESC LIMIT 8"
)])
print("wallet_amount_ccy", [dict(r) for r in qall(
    "SELECT IFNULL(amount_currency,'') ccy, COUNT(*) n FROM wallet_hits GROUP BY 1 ORDER BY n DESC LIMIT 8"
)])
