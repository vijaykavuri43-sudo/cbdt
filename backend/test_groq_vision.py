"""Groq vision slip extract — JSON parse, ref-as-amount sanitize, merge."""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from PIL import Image  # noqa: E402

from . import config  # noqa: E402
from .enrich import (  # noqa: E402
    _groq_extract,
    _merge_vision,
    _parse_model_json,
    _sanitize_vision_fields,
)


def _tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 255, 255)).save(buf, "JPEG")
    return buf.getvalue()


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class GroqVisionTests(unittest.TestCase):
    def test_parse_fenced_json(self):
        d = _parse_model_json(
            '```json\n{"amount_value": 150, "utr": "314965897745"}\n```'
        )
        self.assertEqual(d["amount_value"], 150)
        self.assertEqual(d["utr"], "314965897745")

    def test_sanitize_paytm_ref_not_rupees(self):
        g = _sanitize_vision_fields(
            {"amount_value": 3149658, "utr": "314965897745"},
            "Ref. No: 314965897745 ~ 3149658",
        )
        self.assertIsNone(g["amount_value"])
        self.assertTrue(g.get("ref_as_amount"))

    def test_merge_sets_groq_engine(self):
        out = {"amount_value": None, "amount_inr": None, "engine": "tesseract"}
        _merge_vision(
            out,
            {
                "amount_value": 150,
                "amount_currency": "INR",
                "utr": "314965897745",
                "_vision_engine": "groq",
            },
            "UPI ID: x@ok ₹150 Ref. No: 314965897745",
        )
        self.assertEqual(out["amount_inr"], 150)
        self.assertEqual(out["utr"], "314965897745")
        self.assertIn("groq", out["engine"])

    def test_groq_extract_reads_json_choice(self):
        saved = (config.GROQ_ENABLED, config.GROQ_API_KEY)
        config.GROQ_ENABLED = True
        config.GROQ_API_KEY = "test-groq"

        class _Client:
            def __init__(self, timeout=None):
                pass

            def post(self, url, **kwargs):
                self.last = url
                return _Resp(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": '{"amount_value":3000,"amount_currency":"INR","utr":"757251864413","vpa":"7903709938-1@okbizaxis"}'
                                }
                            }
                        ]
                    },
                )

            def close(self):
                pass

        try:
            with mock.patch("httpx.Client", _Client):
                g = _groq_extract(_tiny_jpeg(), "Paid to FUSO KUMAR")
            self.assertNotIn("_groq_error", g)
            self.assertEqual(g["amount_value"], 3000)
            self.assertEqual(g["utr"], "757251864413")
            self.assertEqual(g["_vision_engine"], "groq")
        finally:
            config.GROQ_ENABLED, config.GROQ_API_KEY = saved

    def test_groq_ocr_slip_merges_without_gemini(self):
        """Officer re-OCR path (2026-09-21): tesseract base + Groq merge only.

        Groq is the quota-holding Vision provider; Gemini must not be called.
        """
        from .enrich import groq_ocr_slip

        from . import config as _config

        saved = _config.GROQ_ENABLED
        _config.GROQ_ENABLED = True
        try:
            base = {
                "kind": "ocr",
                "engine": "tesseract",
                "text_sample": "To TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx",
                "amount_value": None,
            }
            vision = {
                "amount_value": 500,
                "amount_currency": "INR",
                "wallet_address": "TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx",
                "_vision_engine": "groq",
                "_model": "qwen/test",
            }
            with (
                mock.patch("backend.enrich.ocr_slip", return_value=dict(base)),
                mock.patch("backend.enrich._groq_extract", return_value=dict(vision)),
                mock.patch("backend.enrich._gemini_extract") as gx,
            ):
                out = groq_ocr_slip(_tiny_jpeg())
        finally:
            _config.GROQ_ENABLED = saved
        gx.assert_not_called()
        self.assertEqual(out["engine"], "tesseract+groq")
        self.assertEqual(out["confidence"], "high")
        self.assertTrue(
            any(
                w.get("address") == "TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx"
                for w in out.get("wallets", [])
            )
        )


if __name__ == "__main__":
    unittest.main()
