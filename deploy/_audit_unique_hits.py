#!/usr/bin/env python3
import json
import urllib.parse
import urllib.request

tok = open("/root/.local/share/s3-live-monitor/access-token.txt").read().strip()


def get(path: str):
    req = urllib.request.Request(
        "http://127.0.0.1:8787" + path,
        headers={"Authorization": "Bearer " + tok},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


u = get(
    "/api/money-flows-unique?rail_type=crypto_wallet&folder=&since=2026-09-11"
    "&until=2026-09-18&source=object&limit=3"
)
print("unique_total", u.get("rows_total"), "items", len(u.get("items") or []))
for it in (u.get("items") or [])[:3]:
    rid = it.get("rail_id") or ""
    hits = get(
        "/api/money-flows-unique-hits?rail_type=crypto_wallet&rail_id="
        + urllib.parse.quote(rid)
        + "&since=2026-09-11&until=2026-09-18&source=object&limit=50"
    )
    keys = [h.get("source_key") for h in hits.get("items") or []]
    folders = sorted({h.get("folder") for h in hits.get("items") or []})
    print(
        json.dumps(
            {
                "rail_prefix": rid[:10],
                "image_hits": it.get("image_hits"),
                "slips": it.get("slips"),
                "hit_rows": hits.get("rows_total"),
                "folders": folders,
                "sample_keys": keys[:6],
            },
            ensure_ascii=True,
        )
    )

upi = get(
    "/api/money-flows-unique?rail_type=upi&since=2026-09-11&until=2026-09-18"
    "&source=object&limit=1"
)
if upi.get("items"):
    rid = upi["items"][0]["rail_id"]
    h = get(
        "/api/money-flows-unique-hits?rail_type=upi&rail_id="
        + urllib.parse.quote(rid)
        + "&since=2026-09-11&until=2026-09-18&source=object&limit=20"
    )
    print(
        "upi_hits",
        h.get("rows_total"),
        "folders",
        sorted({x.get("folder") for x in h.get("items") or []}),
    )

bank = get(
    "/api/money-flows-unique?rail_type=bank_account&since=2026-09-11"
    "&until=2026-09-18&source=object&limit=1"
)
if bank.get("items"):
    rid = bank["items"][0]["rail_id"]
    h = get(
        "/api/money-flows-unique-hits?rail_type=bank_account&rail_id="
        + urllib.parse.quote(rid)
        + "&since=2026-09-11&until=2026-09-18&source=object&limit=20"
    )
    print(
        "bank_hits",
        h.get("rows_total"),
        "folders",
        sorted({x.get("folder") for x in h.get("items") or []}),
    )
