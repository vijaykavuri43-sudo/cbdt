"""Reference-source coverage: every identified wallet should carry view-proof.

2026-09-17: 84/114 reference rows shipped with no proof link — not because
the bucket lacks the data, but because build-identified-wallets.py only
joined the 2026-09-04 WALLET-PROOF-MAP (image proofs). bankingqr/bank_data/
deposit-JSON/dashboard rows kept only a human-readable source_ref. These
tests pin the two fixes: (1) the self-key rule (a source_ref that already
is a bucket path becomes the proof key), and (2) db.live_keys_for, the
serve-time fallback that attaches live bucket keys to reference rows.
"""

import csv
import json
import os
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["S3LM_DATA"] = _tmp
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp) / "no-references")

from backend import db  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
REF_CSV = REPO / "s3-live-monitor" / "reference" / "identified-wallets.csv"

ADDR = "TAVK2WthV3MuzyRvNLPPyd6GLsUBxp6oVs"


def _seed(key: str, addr: str, last_modified: str):
    c = db.connect()
    c.execute(
        "INSERT INTO objects(key,top_prefix,size,etag,last_modified,seen_utc,"
        "enriched,extract_json) VALUES(?,?,?,?,?,?,1,?)",
        (
            key,
            key.split("/")[0] + "/",
            10,
            "e-" + key,
            last_modified,
            "2026-09-17T00:00:00Z",
            json.dumps({"kind": "qr", "wallets": [{"address": addr}]}),
        ),
    )
    c.commit()


def test_live_keys_for_newest_first_capped():
    _seed("bankingqr/old.png", ADDR, "2026-09-10T00:00:00Z")
    _seed("bankingqr/new.png", ADDR, "2026-09-12T00:00:00Z")
    _seed(
        "qrcode/other.png",
        "0xBBB2220000000000000000000000000000000000",
        "2026-09-11T00:00:00Z",
    )
    db.backfill_wallet_hits()
    db.mark_wallet_index_ready()
    keys = db.live_keys_for([ADDR, "0xnosuchwallet"])
    assert keys[ADDR] == ["bankingqr/new.png", "bankingqr/old.png"], keys
    assert "0xnosuchwallet" not in keys
    assert db.live_keys_for([ADDR], per=1)[ADDR] == ["bankingqr/new.png"]


def test_self_key_rule_single_path_source_refs_have_proof():
    rows = list(csv.DictReader(open(REF_CSV, encoding="utf-8")))
    assert len(rows) == 114
    bad = [
        r["address"]
        for r in rows
        if "/" in (r["source_ref"] or "")
        and " " not in (r["source_ref"] or "")
        and not (r["proof_s3_key"] or "").strip()
    ]
    assert bad == [], f"{len(bad)} single-path source_refs still keyless"


def test_deposit_json_census_has_keys():
    rows = list(csv.DictReader(open(REF_CSV, encoding="utf-8")))
    dep = [r for r in rows if r["source_ref"] == "deposit/1780826098942.json"]
    assert len(dep) == 24
    assert all(r["proof_s3_key"] == "deposit/1780826098942.json" for r in dep)
