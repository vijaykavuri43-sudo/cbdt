"""Transaction CSV must match the complete deduplicated evidence view."""
import csv
import io
import json
from unittest.mock import patch

from . import app as app_module, db
from .test_slip_pdf import database, seed, FILTERS, WALLET  # noqa: F401


def export_rows(**filters):
    # Consume the real CSV writer synchronously; HTTP streaming is covered
    # by the authenticated deployment check.
    with patch.object(app_module, "_export_response",
                      side_effect=lambda rows, cols, name: "".join(app_module._iter_export_csv(cols, rows))):
        body = app_module.export_money_flows(
            **dict(FILTERS, rail_type="crypto_wallet", profile="compact", deduplicate=True, **filters)
        )
    return list(csv.DictReader(io.StringIO(body.lstrip("\ufeff"))))


def test_export_all_groups_not_one_per_address_or_page(database):
    seed(database, "deposit/a.jpg")
    seed(database, "deposit/copy.jpg")
    seed(database, "deposit/b.jpg", tx="b" * 64)
    seed(database, "deposit/missing.jpg", tx="", status="unresolved")
    page = db.crypto_deduplicated_flows(limit=1, **FILTERS)
    rows = export_rows()
    assert len(page["items"]) == 1
    assert len(rows) == page["rows_total"] == 3
    assert {r["transaction hash"] for r in rows} == {"a" * 64, "b" * 64, ""}
    missing = next(r for r in rows if not r["transaction hash"])
    assert missing["hash_status"] == "missing"
    assert missing["tx_status"] == "unresolved"
    duplicate = next(r for r in rows if r["transaction hash"] == "a" * 64)
    assert duplicate["duplicate_count"] == "1"
    assert json.loads(duplicate["source_object_keys"]) == ["deposit/a.jpg", "deposit/copy.jpg"]


def test_export_includes_image_only_slips_and_applies_filters(database):
    seed(database, "deposit/a.jpg")
    seed(database, "withdrawal/b.jpg", tx="b" * 64, direction="withdrawal")
    database.execute(
        "INSERT INTO wallet_hits(address,key,last_modified,tx_status) VALUES (?,?,?,?)",
        (WALLET, "deposit/image.jpg", "2026-11-20T12:00:00Z", "unresolved"),
    )
    rows = export_rows(direction="deposit")
    assert {r["source_object_key"] for r in rows} == {"deposit/a.jpg", "deposit/image.jpg"}
    assert export_rows(q="no-such-wallet") == []
    assert export_rows(tx_status="verified", direction="withdrawal")[0]["transaction hash"] == "b" * 64


def test_exact_content_group_uses_available_verified_hash(database):
    seed(database, "deposit/missing.jpg", tx="", status="unresolved", etag="f" * 32)
    seed(database, "deposit/verified.jpg", tx="c" * 64, etag="f" * 32)
    rows = export_rows()
    assert len(rows) == 1
    assert rows[0]["transaction hash"] == "c" * 64
    assert rows[0]["hash_status"] == "verified"


def test_ocr_hash_symbols_preserve_prefix_and_suffix():
    from .chain import extract_txid_candidates
    for text, prefix, suffix in (
        ("Hash: b4a9béadf7f9ff8cdébcde17...59ab9", "b4a9b6adf7f9ff8cd6bcde17", "59ab9"),
        ("Hash: ¢420ac5175b3762051fd0c... 330b4", "c420ac5175b3762051fd0c", "330b4"),
        ("Hash: 62dd470céa4bb9cf...e2fbf", "62dd470c6a4bb9cf", "e2fbf"),
        ("Hash: aQaaba9fa8f2dad5... d16d8", "a0aaba9fa8f2dad5", "d16d8"),
    ):
        assert any(r["prefix"] == prefix and r["suffix"] == suffix
                   for r in extract_txid_candidates(text))
