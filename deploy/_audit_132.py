"""Why Crypto wallets distinct stays at 132 — sync vs decode vs no-wallet content."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB = Path("/root/.local/share/s3-live-monitor/manifest.db")
IMG = (
    "(ext IN ('.jpg','.jpeg','.png','.webp','.gif','.bmp','.tiff','.tif',"
    "'.heic','.heif','.avif','.jfif') OR (ext='' AND ("
    "lower(key) LIKE '%.jpg' OR lower(key) LIKE '%.jpeg' OR lower(key) LIKE '%.png'"
    " OR lower(key) LIKE '%.webp' OR lower(key) LIKE '%.gif')))"
)


def q(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(c, sql, args=()):
    r = c.execute(sql, args).fetchone()
    return dict(r) if r else {}


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    out: dict = {"db": str(DB), "authorization_ref": "STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING"}

    out["wallet_census"] = one(
        c,
        "SELECT COUNT(*) hits, COUNT(DISTINCT address) distinct_addrs, "
        "MIN(last_modified) oldest_hit, MAX(last_modified) newest_hit, "
        "MAX(seen_utc) newest_seen FROM wallet_hits",
    )

    out["new_distinct_by_day"] = q(
        c,
        "SELECT substr(min_lm,1,10) day, COUNT(*) new_addresses FROM ("
        "  SELECT address, MIN(last_modified) min_lm FROM wallet_hits GROUP BY address"
        ") GROUP BY 1 ORDER BY 1 DESC LIMIT 21",
    )

    out["newest_distinct_first_seen"] = q(
        c,
        "SELECT address, network, min_lm first_seen, hits FROM ("
        "  SELECT address, network, MIN(last_modified) min_lm, COUNT(*) hits "
        "  FROM wallet_hits GROUP BY address"
        ") ORDER BY min_lm DESC LIMIT 8",
    )

    out["objects_total"] = one(
        c,
        "SELECT COUNT(*) n, MAX(seen_utc) newest_seen, MAX(last_modified) newest_lm, "
        "SUM(CASE WHEN seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour') THEN 1 ELSE 0 END) seen_1h, "
        "SUM(CASE WHEN seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day') THEN 1 ELSE 0 END) seen_24h, "
        "SUM(CASE WHEN seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 day') THEN 1 ELSE 0 END) seen_7d, "
        "SUM(CASE WHEN last_modified >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day') THEN 1 ELSE 0 END) lm_24h, "
        "SUM(CASE WHEN last_modified >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 day') THEN 1 ELSE 0 END) lm_7d "
        "FROM objects",
    )

    out["recent_objects_by_prefix_24h"] = q(
        c,
        "SELECT top_prefix, COUNT(*) n, "
        "SUM(CASE WHEN enriched=1 THEN 1 ELSE 0 END) enriched, "
        "SUM(CASE WHEN " + IMG + " THEN 1 ELSE 0 END) images "
        "FROM objects WHERE seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day') "
        "GROUP BY top_prefix ORDER BY n DESC LIMIT 12",
    )

    out["recent_objects_by_prefix_7d"] = q(
        c,
        "SELECT top_prefix, COUNT(*) n, "
        "SUM(CASE WHEN enriched=1 THEN 1 ELSE 0 END) enriched, "
        "SUM(CASE WHEN " + IMG + " THEN 1 ELSE 0 END) images "
        "FROM objects WHERE last_modified >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 day') "
        "GROUP BY top_prefix ORDER BY n DESC LIMIT 12",
    )

    # Wallet-priority pending (bankingqr/qrcode/paymentgateway + hinted chat)
    hint = " OR ".join(
        f"lower(key) LIKE '%{t}%'"
        for t in ("trustapp", "trust_wallet", "usdt", "tron", "wallet", "tether", "binance", "crypto")
    )
    out["wallet_priority_pending"] = one(
        c,
        "SELECT COUNT(*) n FROM objects WHERE (enriched=0 AND extract_json IS NULL) "
        "AND (enrich_retry_utc IS NULL OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        f"AND {IMG} AND (top_prefix IN ('bankingqr/','qrcode/','paymentgateway/') "
        f"OR (top_prefix='chat_file/' AND ({hint})))",
    )

    out["slip_backlog"] = one(
        c,
        "SELECT COUNT(*) n FROM objects WHERE (enriched=0 AND extract_json IS NULL) "
        "AND (enrich_retry_utc IS NULL OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        f"AND {IMG} AND top_prefix IN ('deposit/','withdrawal/','payment/')",
    )

    out["enrichment"] = one(
        c,
        "SELECT COUNT(*) total, SUM(enriched) done, "
        "SUM(CASE WHEN extract_json IS NOT NULL THEN 1 ELSE 0 END) with_payload, "
        "SUM(CASE WHEN enrich_error IS NOT NULL THEN 1 ELSE 0 END) errors, "
        "SUM(CASE WHEN enriched_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour') THEN 1 ELSE 0 END) processed_1h, "
        "SUM(CASE WHEN enriched_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day') THEN 1 ELSE 0 END) processed_24h "
        "FROM objects",
    )

    # Among images enriched in last 7 days: wallet vs UPI vs neither
    out["enriched_7d_content"] = q(
        c,
        "SELECT top_prefix, COUNT(*) n, "
        "SUM(CASE WHEN json_extract(extract_json,'$.wallets[0].address') IS NOT NULL THEN 1 ELSE 0 END) with_wallet, "
        "SUM(CASE WHEN coalesce(json_extract(extract_json,'$.vpa'),'') != '' THEN 1 ELSE 0 END) with_vpa, "
        "SUM(CASE WHEN json_extract(extract_json,'$.wallets[0].address') IS NULL "
        " AND coalesce(json_extract(extract_json,'$.vpa'),'') = '' THEN 1 ELSE 0 END) neither "
        "FROM objects WHERE enriched=1 AND extract_json IS NOT NULL "
        "AND enriched_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 day') "
        f"AND {IMG} AND top_prefix IN ('deposit/','withdrawal/','bankingqr/','qrcode/','chat_file/','payment/') "
        "GROUP BY top_prefix ORDER BY n DESC",
    )

    # Images uploaded/modified last 7d that ARE enriched: same content split
    out["new_images_7d_content"] = q(
        c,
        "SELECT top_prefix, COUNT(*) n, SUM(enriched) enriched, "
        "SUM(CASE WHEN json_extract(extract_json,'$.wallets[0].address') IS NOT NULL THEN 1 ELSE 0 END) with_wallet, "
        "SUM(CASE WHEN coalesce(json_extract(extract_json,'$.vpa'),'') != '' THEN 1 ELSE 0 END) with_vpa "
        "FROM objects WHERE last_modified >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 day') "
        f"AND {IMG} AND top_prefix IN ('deposit/','withdrawal/','bankingqr/','qrcode/','chat_file/','payment/') "
        "GROUP BY top_prefix ORDER BY n DESC",
    )

    out["wallet_hits_7d"] = one(
        c,
        "SELECT COUNT(*) hits, COUNT(DISTINCT address) distinct_addrs "
        "FROM wallet_hits WHERE last_modified >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 day')",
    )
    out["wallet_hits_24h"] = one(
        c,
        "SELECT COUNT(*) hits, COUNT(DISTINCT address) distinct_addrs "
        "FROM wallet_hits WHERE last_modified >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')",
    )

    out["newest_indexed"] = q(
        c,
        "SELECT key, top_prefix, size, last_modified, seen_utc, enriched FROM objects "
        "ORDER BY seen_utc DESC LIMIT 8",
    )

    out["newest_wallet_hits"] = q(
        c,
        "SELECT address, network, key, last_modified FROM wallet_hits "
        "ORDER BY last_modified DESC LIMIT 8",
    )

    # bankingqr coverage — this is the wallet QR rail
    out["bankingqr"] = one(
        c,
        "SELECT COUNT(*) n, SUM(enriched) enriched, "
        "SUM(CASE WHEN json_extract(extract_json,'$.wallets[0].address') IS NOT NULL THEN 1 ELSE 0 END) with_wallet "
        "FROM objects WHERE top_prefix='bankingqr/' AND " + IMG,
    )

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
