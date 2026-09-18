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


if __name__ == "__main__":
    unittest.main()
