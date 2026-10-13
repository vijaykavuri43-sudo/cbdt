"""Agent & operator identity surface (db.agent_network)."""

import os
import tempfile
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db  # noqa: E402


def _seed(cur, rows):
    for r in rows:
        cur.execute(
            "INSERT INTO gateway_rows(key,row_no,kind,customer,amount,website,client,actionby,ts,is_test)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            r,
        )


def test_agent_network_returns_desks_and_summaries(monkeypatch):
    con = db.connect()
    rows = [
        (
            "k",
            1,
            "gateway_deposit",
            "c1",
            100.0,
            "w1",
            "cl1",
            "trader01",
            "01/01/2026 10:00:00",
            0,
        ),
        (
            "k",
            2,
            "gateway_deposit",
            "c2",
            250.5,
            "w1",
            "cl2",
            "trader01",
            "02/01/2026 10:00:00",
            0,
        ),
        (
            "k",
            3,
            "gateway_deposit",
            "c3",
            10.0,
            "w1",
            "cl1",
            "trader04",
            "03/01/2026 10:00:00",
            1,
        ),
        (
            "k",
            4,
            "gateway_payout",
            "c4",
            99.0,
            "w1",
            "cl1",
            "trader01",
            "04/01/2026 10:00:00",
            0,
        ),
        (
            "k",
            5,
            "gateway_deposit",
            "c5",
            5.0,
            "w1",
            "cl1",
            "  ",
            "05/01/2026 10:00:00",
            0,
        ),
    ]
    _seed(con, rows)
    try:
        out = db.agent_network(limit=10, offset=0)
    finally:
        con.execute("DELETE FROM gateway_rows WHERE row_no<=5")
        con.commit()

    desks = {d["operator"]: d for d in out["operator_desks"]}
    assert set(desks) == {"trader01", "trader04"}, desks
    # payout rows must not inflate deposit totals
    assert desks["trader01"]["rows"] == 2
    assert desks["trader01"]["deposit_amount_inr"] == 350.5
    assert desks["trader01"]["clients"] == 2
    assert desks["trader01"]["test_rows"] == 0
    assert desks["trader04"]["test_rows"] == 1
    assert out["summary"]["operator_consoles"] == 2


def test_agent_network_tolerates_missing_artifacts(monkeypatch):
    # No roster artifact on this box => must still serve live desks, never raise.
    monkeypatch.setattr(db, "_intel_artifact", lambda rel: None)
    out = db.agent_network(limit=5, offset=0)
    assert out["summary"]["agents"] == 0
    assert out["items"] == []
    assert out["total"] == 0
    assert out["negative_results"] == []


def test_agent_network_counts_from_roster(monkeypatch):
    roster = {
        "agents": [
            {
                "username": "a1",
                "mobiles": ["9000000001"],
                "origins": ["https://x.com"],
                "upline_managers": ["mgr"],
                "child_accounts": 5,
            },
            {
                "username": "a2",
                "mobiles": ["9000000001", "9000000002"],
                "origins": ["https://x.com"],
                "upline_managers": ["mgr"],
                "child_accounts": 1,
            },
            {
                "username": "a3",
                "mobiles": [],
                "origins": [],
                "upline_managers": [],
                "child_accounts": 0,
            },
        ],
        "source": "test",
        "collected_utc": "2026-10-13T00:00:00+00:00",
    }
    findings = {
        "agents_with_login_records": [{"username": "a1", "mobile": "9000000001"}],
        "negative_results": [{"id": "NEG-1", "result": "refuted"}],
        "unmined_corpora": [{"pattern": "data_*", "files": 2}],
    }
    monkeypatch.setattr(
        db,
        "_intel_artifact",
        lambda rel: roster if "agent_roster" in rel else findings,
    )
    out = db.agent_network(limit=10, offset=0)
    s = out["summary"]
    assert s["agents"] == 3
    assert s["agents_with_mobile"] == 2
    assert s["distinct_mobiles"] == 2
    # 9000000001 is registered to two agent accounts
    assert s["mobiles_shared_by_multiple_agents"] == 1
    assert s["upline_managers"] == 1
    assert s["brand_origins"] == 1
    assert s["agents_with_login_evidence"] == 1
    assert out["top_upline_managers"][0] == {"name": "mgr", "agents": 2}
    assert len(out["negative_results"]) == 1
    assert out["items"][0]["username"] == "a2"
    # ranked by mobile count first => a2 (2 mobiles) outranks a1 (1)
    assert [r["username"] for r in out["items"]][:2] == ["a2", "a1"]


def test_agent_network_pagination_slices(monkeypatch):
    roster = {
        "agents": [
            {
                "username": f"a{i}",
                "mobiles": [],
                "origins": [],
                "upline_managers": [],
                "child_accounts": 0,
            }
            for i in range(10)
        ]
    }
    monkeypatch.setattr(
        db, "_intel_artifact", lambda rel: roster if "agent_roster" in rel else None
    )
    page = db.agent_network(limit=3, offset=6)
    assert page["total"] == 10
    assert [r["username"] for r in page["items"]] == ["a6", "a7", "a8"]


def test_agent_network_carries_guard_rails(monkeypatch):
    monkeypatch.setattr(
        db,
        "_intel_artifact",
        lambda rel: (
            {"guard_rail": "mobiles are KYC contact data"}
            if "staff_identity" in rel
            else None
        ),
    )
    out = db.agent_network(limit=1, offset=0)
    assert out["guard_rail"] == "mobiles are KYC contact data"
    assert "corroboration" in out["scope_note"]
    assert out["assessment_fact_type"] == "analyst_conclusion"
    assert out["fact_type"] == "fact"
