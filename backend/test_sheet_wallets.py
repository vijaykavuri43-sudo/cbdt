"""Root-cause regression: spreadsheet wallets must reach the Crypto tab.

2026-09-16: the 28 hawala wallets lived only in bank_data xlsx cells that no
pipeline ever scanned (ledger indexed money rows; wallet extractor is
image-only) — and a naive merge deadlocked (Lock re-entry inside
sync_ledger). These tests pin: sheet scan surfaces wallets, merges never
clobber image payloads, and sync_ledger completes (no deadlock).
"""

import io
import json
import os
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["S3LM_DATA"] = _tmp
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp) / "no-references")

from backend import db, ledger  # noqa: E402

HAWALA = "TEBVmoB9YvjcjhhJBAf5dNyQqEVk9e7CUU"
CR7 = "TQbXYxYw3NtKLaHbKEWtu1xMfPbdD3A5Ef"


def _seed(key: str, data: bytes):
    c = db.connect()
    c.execute(
        "INSERT INTO objects(key,top_prefix,size,etag,last_modified,seen_utc,"
        "enriched,extract_json) VALUES(?,?,?,?,?,?,0,NULL)",
        (
            key,
            key.split("/")[0] + "/",
            len(data),
            "e1",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    c.commit()


class _Store:
    def __init__(self, data: bytes):
        self._data = data

    def get_object_stream(self, key):
        data = self._data

        class _R:
            def read(self):
                return data

            def close(self):
                pass

        return _R(), {}


def test_sheet_csv_wallet_reaches_feed_and_distinct():
    data = (f"account,wallet,amount\nSBIN0030490,{HAWALA},98784\n").encode()
    _seed("csv-files/bank_data_rc.csv", data)
    out = ledger.sync_ledger(_Store(data), limit_files=5)
    assert out["errors"] == [], out
    assert out.get("sheet_wallets_added", 0) >= 1, out
    assert any(r.get("address") == HAWALA for r in db.wallet_feed(50, 0))
    assert any(r.get("address") == HAWALA for r in db.wallet_distinct(50, 0))


def test_merge_never_clobbers_image_payload():
    c = db.connect()
    c.execute(
        "UPDATE objects SET extract_json=? WHERE key=?",
        (
            json.dumps(
                {"kind": "deposit", "wallets": [{"address": CR7, "network": "TRON"}]}
            ),
            "csv-files/bank_data_rc.csv",
        ),
    )
    c.commit()
    added = db.merge_object_wallets(
        "csv-files/bank_data_rc.csv", [{"address": HAWALA, "network": "TRON"}], "sheet"
    )
    assert added == 1
    p = json.loads(
        c.execute(
            "SELECT extract_json FROM objects WHERE key=?",
            ("csv-files/bank_data_rc.csv",),
        ).fetchone()[0]
    )
    assert p["kind"] == "deposit"
    assert {w["address"] for w in p["wallets"]} == {CR7, HAWALA}
    # re-merge same address: zero new, payload untouched
    assert (
        db.merge_object_wallets(
            "csv-files/bank_data_rc.csv",
            [{"address": HAWALA, "network": "TRON"}],
            "sheet",
        )
        == 0
    )
