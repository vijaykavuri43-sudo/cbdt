"""Overlap: live distinct vs identified census + source path quality."""
from collections import Counter
from backend import db
from backend.evidence import load_reference_wallets

c = db.connect()
live_rows = c.execute(
    "SELECT DISTINCT address, key FROM wallet_hits WHERE address!=''"
).fetchall()
live = {r["address"] for r in live_rows}
pref = Counter()
by_addr = {}
for r in live_rows:
    k = r["key"] or ""
    folder = k.split("/")[0] if "/" in k else "(no-folder)"
    pref[folder] += 1
    by_addr.setdefault(r["address"], []).append(k)

ref = load_reference_wallets()
ref_addrs = {r["address"] for r in ref}
overlap = live & ref_addrs
print("live_distinct", len(live))
print("census", len(ref_addrs))
print("overlap", len(overlap))
print("live_only", len(live - ref_addrs))
print("census_only", len(ref_addrs - live))
print("wallet_hits_by_folder", dict(pref))

dash = []
for r in ref:
    src = r.get("source_ref") or ""
    pk = r.get("proof_s3_key") or ""
    if "api/wallet" in src.lower() or src.lower().startswith("dashboard"):
        live_keys = by_addr.get(r["address"]) or []
        dash.append(
            {
                "address": r["address"],
                "source_ref": src,
                "proof_s3_key": pk,
                "in_live": r["address"] in live,
                "live_keys": live_keys[:3],
            }
        )
print("census_dashboard_source_ref_rows", len(dash))
for d in dash:
    print(
        "DASH",
        d["address"],
        "in_live=" + str(d["in_live"]),
        "proof=" + (d["proof_s3_key"] or "-"),
        "live=" + "|".join(d["live_keys"][:2]),
        "ref=" + d["source_ref"][:80],
    )
