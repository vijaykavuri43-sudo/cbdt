"""Dashboard counts must be exact without reading the full OCR payload table."""

import json
import sqlite3
from unittest.mock import patch

import pytest

from backend import db


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    db._migrate(c)
    with patch.object(db, "connect", return_value=c):
        yield c
    c.close()


def put(c, key, payload=None, **kw):
    fields = dict(
        key=key, top_prefix=key.split("/")[0] + "/", size=123,
        ext=db.file_ext(key), enriched=int(payload is not None),
        extract_json=json.dumps(payload) if payload is not None else None,
    )
    fields.update(kw)
    c.execute(
        f"INSERT INTO objects({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(fields.values()),
    )


def old_totals(c):
    rows = c.execute(
        "SELECT json_extract(extract_json,'$.kind') k,COUNT(*) c,"
        "SUM(CASE WHEN json_extract(extract_json,'$.amount_inr') BETWEEN 0.01 AND ?"
        " THEN json_extract(extract_json,'$.amount_inr') END) amt"
        " FROM objects NOT INDEXED WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind') IN ('deposit','withdrawal') GROUP BY k",
        (db.MAX_AMOUNT_INR,),
    )
    out = {kind: {"total": 0, "sum_amount_inr": 0} for kind in ("deposit", "withdrawal")}
    for r in rows:
        out[r["k"]] = {"total": r["c"], "sum_amount_inr": r["amt"] or 0}
    return out


def test_flow_totals_match_original_including_invalid_amounts(conn):
    for i, amount in enumerate((None, False, True, 0, -1, 0.01, 100, "250", db.MAX_AMOUNT_INR,
                                db.MAX_AMOUNT_INR + 1, "not-money")):
        for kind in ("deposit", "withdrawal", "qr"):
            put(conn, f"{kind}/{i}.jpg", {"kind": kind, "amount_inr": amount})
    put(conn, "deposit/pending.jpg")
    assert db.flow_kind_totals() == old_totals(conn)


def test_flow_index_tracks_reocr_and_delete(conn):
    put(conn, "deposit/a.jpg", {"kind": "deposit", "amount_inr": 100})
    assert db.flow_kind_totals()["deposit"]["sum_amount_inr"] == 100
    conn.execute("UPDATE objects SET extract_json=? WHERE key=?", (
        json.dumps({"kind": "withdrawal", "amount_inr": 250}), "deposit/a.jpg",
    ))
    assert db.flow_kind_totals() == old_totals(conn)
    assert db.flow_kind_totals()["withdrawal"]["total"] == 1
    conn.execute("DELETE FROM objects")
    assert db.flow_kind_totals() == old_totals(conn)


def test_progress_preserves_counts_and_uses_small_index(conn):
    put(conn, "deposit/a.jpg", {"kind": "deposit"}, enriched_utc="2999-01-01T00:00:00Z")
    put(conn, "other/error.jpg", enrich_error="fetch failed")
    put(conn, "other/b.jpg")
    sqls = []
    conn.set_trace_callback(sqls.append)
    progress = db.enrichment_progress()
    assert progress["total"] == 3
    assert progress["enriched_done"] == progress["with_payload"] == 1
    assert progress["errors"] == progress["processed_last_hour"] == 1
    query = next(q for q in sqls if "idx_obj_progress" in q)
    plan = conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("idx_obj_progress" in r["detail"] for r in plan)


def test_inventory_preserves_category_precedence_and_uses_covering_index(conn):
    keys = ("deposit/a.mp4", "bankingqr/b.jpg", "chat_file/c.mp4", "chat_file/d.mp3",
            "other/e.MP4", "other/f.xlsx", "other/g.pdf", "other/h.apk", "other/i.log")
    for key in keys:
        put(conn, key)
    sqls = []
    conn.set_trace_callback(sqls.append)
    census = db.type_census()
    counts = {c["id"]: c["count"] for c in census["categories"]}
    for cat in ("deposit_withdrawal_slips", "qr_codes", "chat_screenshots",
                "voice_notes", "video", "spreadsheets", "documents", "apps", "logs"):
        assert counts[cat] == 1
    assert sum(counts.values()) == len(keys)
    query = next(q for q in sqls if "idx_obj_inventory" in q)
    plan = conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("COVERING INDEX idx_obj_inventory" in r["detail"] for r in plan)


def test_flow_totals_plan_uses_index_without_json_parsing(conn):
    sqls = []
    conn.set_trace_callback(sqls.append)
    db.flow_kind_totals()
    query = next(q for q in sqls if "idx_obj_flow_totals" in q)
    plan = conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("idx_obj_flow_totals" in r["detail"] for r in plan)
    instructions = conn.execute("EXPLAIN " + query).fetchall()
    assert not any("json_extract" in str(r["p4"]) for r in instructions)


def test_migration_is_repeatable_for_existing_database():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # Existing objects stay intact when the idempotent schema is reapplied.
    c.executescript(db.SCHEMA)
    put(c, "deposit/old.jpg", {"kind": "deposit", "amount_inr": 123})
    c.executescript(db.SCHEMA)
    db._migrate(c)
    db._migrate(c)
    with patch.object(db, "connect", return_value=c):
        assert db.flow_kind_totals() == old_totals(c)
    c.close()
