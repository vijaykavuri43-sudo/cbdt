from .slip_dedup import deduplicate_slips


def row(key, **values):
    return dict(source_key=key, rail_id="T" + "B" * 33, txid="a" * 64,
                tx_status="verified", network="TRON", **values)


def test_same_transaction_under_different_names():
    out = deduplicate_slips([row("deposit/a.jpg"), row("chat_file/b.jpg")])
    assert len(out) == 1
    assert out[0]["duplicate_count"] == 1
    assert len(out[0]["source_keys"]) == 2


def test_same_wallet_amount_is_not_a_duplicate():
    a, b = row("a", amount_inr=100), row("b", amount_inr=100)
    b["txid"] = "b" * 64
    assert len(deduplicate_slips([a, b])) == 2


def test_unresolved_or_ambiguous_hash_is_not_proof():
    a, b = row("a"), row("b")
    a["tx_status"] = b["tx_status"] = "ambiguous"
    assert len(deduplicate_slips([a, b])) == 2


def test_content_identity_without_transaction():
    a, b = row("a"), row("b")
    for r in (a, b):
        r.update(txid="", object_etag="c" * 32, object_size=100)
    assert len(deduplicate_slips([a, b])) == 1
    b["object_etag"] = "c" * 32 + "-2"
    assert len(deduplicate_slips([a, b])) == 2


def test_transitive_identity_preserves_all_sources():
    a, b, c = row("a"), row("b"), row("c")
    b["pixel_sha256"] = c["pixel_sha256"] = "pixels"
    c["txid"] = "b" * 64
    out = deduplicate_slips([a, b, c])
    assert len(out) == 1 and out[0]["source_keys"] == ["a", "b", "c"]


def test_chains_are_separate():
    a, b = row("a"), row("b")
    for r in (a, b):
        r["rail_id"] = "0x" + "1" * 40
    a["network"], b["network"] = "ethereum", "bsc"
    assert len(deduplicate_slips([a, b])) == 2
