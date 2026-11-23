"""Conservative evidence grouping. Originals and their provenance are retained."""

import re


def transaction_identity(row):
    txid = str(row.get("txid") or "").strip().lower().removeprefix("0x")
    if row.get("tx_status") not in ("verified", "derived") or not re.fullmatch(r"[0-9a-f]{64}", txid):
        return None
    network = str(row.get("network") or "").lower().replace(" ", "")
    rail = str(row.get("rail_id") or "")
    if rail.startswith("T") or "tron" in network or "trc20" in network:
        network = "tron"
    # Do not conflate matching hashes on different EVM chains.
    return (network or "unknown", txid)


def deduplicate_slips(rows):
    """Union proven transaction/content identities before pagination.

    Never group by wallet, amount, filename similarity or perceptual similarity.
    A single-part S3 ETag + size is a content-identity hint; byte/pixel SHA-256
    fingerprints, when available, also join exact copies. No files are removed.
    """
    rows = [dict(r) for r in rows]
    parent = list(range(len(rows)))
    identities = {}
    reasons = {}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, row in enumerate(rows):
        tokens = [("key", row.get("source_key") or row.get("key") or str(i))]
        tx = transaction_identity(row)
        if tx:
            tokens.append(("transaction", *tx))
        etag = str(row.get("object_etag") or "").strip('"').lower()
        size = int(row.get("object_size") or 0)
        if size and re.fullmatch(r"[0-9a-f]{32}", etag):
            tokens.append(("content-etag", size, etag))
        for field in ("content_sha256", "pixel_sha256"):
            value = row.get(field)
            if value:
                tokens.append((field, value))
        reasons[i] = set()
        for token in tokens:
            if token in identities:
                previous = identities[token]
                parent[root(i)] = root(previous)
                reasons[i].add(token[0])
                reasons[previous].add(token[0])
            else:
                identities[token] = i

    grouped = {}
    for i, row in enumerate(rows):
        grouped.setdefault(root(i), []).append((i, row))
    out = []
    for group in grouped.values():
        representative = max(group, key=lambda pair: (
            {"verified": 3, "derived": 2}.get(pair[1].get("tx_status"), 0),
            int(pair[1].get("object_size") or 0),
            pair[1].get("last_modified") or "", pair[1].get("source_key") or "",
        ))[1].copy()
        keys = sorted({key for _, row in group for key in row.get("source_keys", [row.get("source_key")]) if key})
        representative["source_keys"] = keys
        representative["duplicate_count"] = max(0, len(keys) - 1)
        representative["dedup_reasons"] = sorted({reason for i, row in group for reason in reasons[i] | set(row.get("dedup_reasons", []))})
        representative["rail_ids"] = sorted({rail for _, row in group for rail in row.get("rail_ids", [row.get("rail_id")]) if rail})
        representative["source_gamblers"] = sorted({name for _, r in group for name in r.get("source_gamblers", [r.get("gambler")]) if name})
        representative["group_transaction_ids"] = sorted({tx for _, r in group for tx in r.get("group_transaction_ids", [r.get("txid")]) if tx})
        representative["origins"] = sorted({origin for _, row in group for origin in row.get("origins", [])})
        out.append(representative)
    return sorted(out, key=lambda r: (r.get("last_modified") or "", r.get("source_key") or ""), reverse=True)
