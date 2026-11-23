"""Query-plan and result regressions; isolated from the evidence database."""

import sqlite3

import pytest

from . import db


@pytest.fixture
def connection(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    db._migrate(c)
    monkeypatch.setattr(db, "connect", lambda: c)
    yield c
    c.close()


def seed(c):
    wallets = ["T" + ch * 33 for ch in "BCD"]
    for wallet in wallets[:2]:
        c.execute(
            "INSERT INTO payment_transactions"
            "(source_key,direction,rail_type,rail_id,last_modified,amount_inr,tx_status)"
            " VALUES ('deposit/shared.jpg','deposit','crypto_wallet',?,"
            "'2026-11-20T12:00:00Z',100,'verified')", (wallet,),
        )
    for wallet in (wallets[0], wallets[2]):
        c.execute(
            "INSERT INTO wallet_hits(address,key,last_modified,tx_status)"
            " VALUES (?,'deposit/image.jpg','2026-11-20T12:00:00Z','verified')",
            (wallet,),
        )
    return wallets


def test_crypto_totals_pagination_and_empty_window(connection):
    wallets = seed(connection)
    params = dict(rail_type="crypto_wallet", since="2026-11-16", until="2026-11-23")
    out = db.money_flows_unique(**params, limit=1)
    assert out["rows_total"] == out["distinct_rails"] == 3
    assert out["transactions"] == 1  # not two: shared slip has two wallets
    assert out["image_hits"] == out["decoded_wallets"] == 2
    assert out["items"][0]["rail_id"] == wallets[0]
    assert not {"total", "hits", "addrs"} & out["items"][0].keys()
    all_items = db.money_flows_unique(**params)["items"]
    assert db.money_flows_unique(**params, limit=1, offset=1)["items"] == all_items[1:2]
    beyond = db.money_flows_unique(**params, offset=10)
    assert beyond["items"] == []
    assert beyond["rows_total"] == 3
    assert beyond["transactions"] == 1
    empty = db.money_flows_unique(rail_type="crypto_wallet", since="2027-01-01")
    assert empty["items"] == []
    for field in ("rows_total", "distinct_rails", "transactions", "image_hits", "decoded_wallets"):
        assert empty[field] == 0


@pytest.mark.parametrize("direction,folder,status,expected", [
    ("deposit", "deposit", "verified", 3),
    ("withdrawal", "", "", 0),
    ("both", "withdrawal", "", 0),
    ("both", "", "unresolved", 0),
    ("both", "", "unchecked", 0),
])
def test_crypto_filters(connection, direction, folder, status, expected):
    seed(connection)
    out = db.money_flows_unique(
        rail_type="crypto_wallet", direction=direction, folder=folder,
        tx_status=status, since="2026-11-16", until="2026-11-23",
    )
    assert out["rows_total"] == len(out["items"]) == expected


def test_crypto_aggregates_scan_base_tables_only_once(connection):
    seed(connection)
    statements = []
    connection.set_trace_callback(statements.append)
    db.money_flows_unique(rail_type="crypto_wallet", since="2026-11-16", until="2026-11-23")
    connection.set_trace_callback(None)
    sql = " ".join(statements)
    # One distinct-slip count + one per-wallet aggregate; no repeated UNION
    # scans for counts, pagination and joins.
    assert sql.count("FROM payment_transactions") == 2
    assert sql.count("FROM wallet_hits") == 1


def test_date_window_uses_type_and_date_index(connection):
    where, args = db._money_flow_filters(
        direction="both", rail_type="crypto_wallet",
        since="2026-11-16", until="2026-11-23",
    )
    plan = " ".join(row["detail"] for row in connection.execute(
        f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM payment_transactions WHERE {where}", args,
    ))
    assert "idx_pay_rail_lm_direction" in plan
    assert "last_modified>? AND last_modified<?" in plan


def test_exact_identifier_probe_uses_partial_indexes(connection):
    seed(connection)
    statements = []
    connection.set_trace_callback(statements.append)
    db._money_flow_filters(direction="both", q="T" + "B" * 33)
    connection.set_trace_callback(None)
    probe = next(s for s in statements if s.startswith("SELECT 1 FROM payment_transactions"))
    plan = " ".join(row["detail"] for row in connection.execute("EXPLAIN QUERY PLAN " + probe))
    assert "MULTI-INDEX OR" in plan
    assert "SCAN payment_transactions" not in plan


def test_noncrypto_total_matches_distinct_rails(connection):
    for rail, address in (("upi", "alice@ybl"), ("bank_account", "123456789012")):
        for i in range(2):
            connection.execute(
                "INSERT INTO payment_transactions"
                "(source_key,direction,rail_type,rail_id,last_modified) VALUES (?,?,?,?,?)",
                (f"deposit/{rail}{i}.jpg", "deposit", rail, address, "2026-11-20T00:00:00Z"),
            )
    out = db.money_flows_unique(since="2026-11-16", until="2026-11-23")
    assert out["rows_total"] == out["distinct_rails"] == len(out["items"]) == 2
    assert out["transactions"] == 4
