"""Regression tests for source-of-truth wallet corrections (CASE-2026-CBTF).

2026-09-21: officer-verified image reads may replace stored OCR-degraded
wallet strings ONLY under strict math proof (checksum-valid to-addr,
from-addr present in row); non-wallet fragments (QR payload shards, txid
middles) drop ONLY with recorded evidence. And the officer re-OCR path
must never touch Gemini (Groq holds the quota; tesseract is primary).
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db  # noqa: E402
from . import enrich as _enrich  # noqa: E402
from .enrich import _tron_checksum_ok  # noqa: E402

REAL_TRON = "TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx"
DEGRADED = REAL_TRON.replace("2Uo9uu", "2UO9uu", 1)
FRAG = "33f73f8b55b91f2e3acf466ab991"


def _seed(key, wallets):
    db.upsert_objects(
        [
            {
                "key": key,
                "size": 100,
                "last_modified": "2026-09-21T00:00:00Z",
                "etag": "e1",
            }
        ]
    )
    db.mark_enriched(
        key,
        {
            "kind": "deposit",
            "engine": "tesseract",
            "text_sample": "To " + " ".join(w["address"] for w in wallets),
            "wallets": wallets,
        },
    )


def _wallets_of(key):
    row = (
        db.connect()
        .execute("SELECT extract_json FROM objects WHERE key=?", (key,))
        .fetchone()
    )
    return json.loads(row[0])


def _tron_wallet(addr, checksum="invalid"):
    return {
        "address": addr,
        "network": "TRON",
        "standard": "TRC-20",
        "confidence": "medium",
        "checksum": checksum,
        "role": "unknown",
        "role_confidence": "low",
    }


class ApplySourceReadsTests(unittest.TestCase):
    def test_replace_invalid_with_checksum_valid_twin(self):
        key = "deposit/source-read-replace.jpg"
        _seed(key, [_tron_wallet(DEGRADED)])
        res = db.apply_source_wallet_reads(
            [
                {
                    "key": key,
                    "replace": [{"from": DEGRADED, "to": REAL_TRON}],
                    "provenance": "local-tesseract-v3 test",
                }
            ],
            dry_run=False,
        )
        self.assertEqual(res["applied"], 1)
        self.assertEqual(res["wallets_replaced"], 1)
        p = _wallets_of(key)
        self.assertEqual([w["address"] for w in p["wallets"]], [REAL_TRON])
        self.assertEqual(p["wallets"][0]["checksum"], "valid")
        self.assertTrue(p["wallets"][0].get("source_read"))
        self.assertIn("source_wallet_reads", p)
        hits = (
            db.connect()
            .execute("SELECT address FROM wallet_hits WHERE key=?", (key,))
            .fetchall()
        )
        self.assertEqual([r[0] for r in hits], [REAL_TRON])

    def test_replace_rejects_checksum_failure_no_write(self):
        key = "deposit/source-read-bad-to.jpg"
        _seed(key, [_tron_wallet(DEGRADED)])
        bad = DEGRADED[:-1] + ("0" if DEGRADED[-1] != "0" else "1")
        self.assertFalse(_tron_checksum_ok(bad))
        res = db.apply_source_wallet_reads(
            [{"key": key, "replace": [{"from": DEGRADED, "to": bad}]}],
            dry_run=False,
        )
        self.assertEqual(res["applied"], 0)
        self.assertEqual(len(res["skipped"]), 1)
        p = _wallets_of(key)
        self.assertEqual([w["address"] for w in p["wallets"]], [DEGRADED])

    def test_replace_rejects_from_not_in_row(self):
        key = "deposit/source-read-bad-from.jpg"
        _seed(key, [_tron_wallet(REAL_TRON, checksum="valid")])
        res = db.apply_source_wallet_reads(
            [{"key": key, "replace": [{"from": DEGRADED, "to": REAL_TRON}]}],
            dry_run=False,
        )
        self.assertEqual(res["applied"], 0)
        self.assertTrue(res["skipped"])

    def test_add_requires_checksum_proof(self):
        key = "deposit/source-read-add.jpg"
        _seed(key, [_tron_wallet(REAL_TRON, checksum="valid")])
        res = db.apply_source_wallet_reads(
            [{"key": key, "add": [DEGRADED]}],
            dry_run=False,
        )
        self.assertEqual(res["applied"], 0)
        self.assertTrue(res["skipped"])
        p = _wallets_of(key)
        self.assertEqual(len(p["wallets"]), 1)

    def test_drop_needs_evidence_and_removes(self):
        key = "deposit/source-read-drop.jpg"
        _seed(
            key,
            [
                _tron_wallet(REAL_TRON, checksum="valid"),
                {
                    "address": FRAG,
                    "network": "BITCOIN",
                    "standard": "BTC",
                    "confidence": "high",
                    "checksum": "",
                    "role": "unknown",
                    "role_confidence": "low",
                },
            ],
        )
        no_ev = db.apply_source_wallet_reads(
            [
                {
                    "key": key,
                    "drop": [
                        {"address": FRAG, "reason": "txid_fragment", "evidence": ""}
                    ],
                }
            ],
            dry_run=False,
        )
        self.assertEqual(no_ev["applied"], 0)
        yes_ev = db.apply_source_wallet_reads(
            [
                {
                    "key": key,
                    "drop": [
                        {
                            "address": FRAG,
                            "reason": "txid_fragment",
                            "evidence": "TxHash middle on MEXC TRC20 receipt",
                        }
                    ],
                    "provenance": "fulltext-deposit_1785260983361.png.txt",
                }
            ],
            dry_run=False,
        )
        self.assertEqual(yes_ev["wallets_dropped"], 1)
        p = _wallets_of(key)
        self.assertEqual([w["address"] for w in p["wallets"]], [REAL_TRON])

    def test_dry_run_writes_nothing(self):
        key = "deposit/source-read-dry.jpg"
        _seed(key, [_tron_wallet(DEGRADED)])
        res = db.apply_source_wallet_reads(
            [
                {
                    "key": key,
                    "replace": [{"from": DEGRADED, "to": REAL_TRON}],
                }
            ],
            dry_run=True,
        )
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["applied"], 1)
        p = _wallets_of(key)
        self.assertEqual([w["address"] for w in p["wallets"]], [DEGRADED])

    def test_evm_case_restore_needs_same_hex_and_provenance(self):
        key = "chat_file/source-read-evm.jpg"
        lower = "0x7aeeca39f6c3e3fbce2f6a1a4bee2215dcc97f7d"
        cased = "0x7AEecA39f6c3e3FbCE2F6A1a4BEE2215DCc97F7D"
        _seed(
            key,
            [
                {
                    "address": lower,
                    "network": "EVM",
                    "standard": "BEP-20",
                    "confidence": "medium",
                    "checksum": "",
                    "role": "beneficiary",
                    "role_confidence": "high",
                }
            ],
        )
        # Different hex must be refused.
        other = "0x8AEecA39f6c3e3FbCE2F6A1a4BEE2215DCc97F7D"
        res = db.apply_source_wallet_reads(
            [
                {
                    "key": key,
                    "replace": [{"from": lower, "to": other}],
                    "provenance": "x",
                }
            ],
            dry_run=False,
        )
        self.assertEqual(res["applied"], 0)
        # Same hex without provenance must be refused.
        res = db.apply_source_wallet_reads(
            [{"key": key, "replace": [{"from": lower, "to": cased}]}],
            dry_run=False,
        )
        self.assertEqual(res["applied"], 0)
        # Same hex + unanimous-read provenance restores casing only.
        res = db.apply_source_wallet_reads(
            [
                {
                    "key": key,
                    "replace": [{"from": lower, "to": cased}],
                    "provenance": "local-tesseract 4/4 unanimous, BNB receive screen",
                }
            ],
            dry_run=False,
        )
        self.assertEqual(res["wallets_replaced"], 1)
        p = _wallets_of(key)
        self.assertEqual([w["address"] for w in p["wallets"]], [cased])
        self.assertTrue(p["wallets"][0].get("evm_case_restored"))
        self.assertEqual(p["wallets"][0]["network"], "EVM")


class GroqPathNeverTouchesGeminiTests(unittest.TestCase):
    def test_groq_slip_runs_tesseract_only_on_quota_failure(self):
        from PIL import Image
        import io

        img = Image.new("L", (64, 16), color=255)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        calls = {}
        orig_ocr = _enrich.ocr_slip

        def _spy(data, allow_gemini=True):
            calls["allow_gemini"] = allow_gemini
            return orig_ocr(data, allow_gemini=allow_gemini)

        orig_gemini = _enrich._gemini_extract
        orig_groq = _enrich._groq_extract
        orig_flag = _enrich.config.GROQ_ENABLED
        _enrich.ocr_slip = _spy
        _enrich.config.GROQ_ENABLED = True
        _enrich._gemini_extract = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("Gemini must not be called on the Groq path")
        )
        _enrich._groq_extract = lambda *a, **k: {"_groq_error": "429 quota-dead"}
        try:
            out = _enrich.groq_ocr_slip(buf.getvalue())
        finally:
            _enrich.ocr_slip = orig_ocr
            _enrich._gemini_extract = orig_gemini
            _enrich._groq_extract = orig_groq
            _enrich.config.GROQ_ENABLED = orig_flag
        self.assertIs(calls.get("allow_gemini"), False)
        self.assertEqual(out.get("_groq_error"), "429 quota-dead")
        self.assertNotIn("_gemini_error", out)


if __name__ == "__main__":
    unittest.main()
