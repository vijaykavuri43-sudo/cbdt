"""Tests for the Groq Cloud text failover in the officer AI assistant.

Failover order per question: Gemini primary -> Groq (text-only) -> offline.
Image ("show this image") questions must NOT fall over to Groq — none of the
available Groq models do vision.
"""

import unittest

from . import aiask, config
from . import enrich


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self):
        self.urls = []

    def post(self, url, **kwargs):
        self.urls.append(url)
        if "groq.com" in url:
            return _Resp(
                200,
                {"choices": [{"message": {"content": "groq answer"}}]},
            )
        return _Resp(429, text='{"error": {"message": "quota"}}')


class GroqFailoverTests(unittest.TestCase):
    def setUp(self):
        self._saved = (
            config.GEMINI_ENABLED,
            config.GROQ_ENABLED,
            config.GROQ_API_KEY,
            config.GROQ_MODEL,
            config.GROQ_FALLBACK_MODEL,
            enrich._gemini_client,
        )
        config.GEMINI_ENABLED = True
        config.GEMINI_API_KEY = "test-gemini"
        config.GROQ_ENABLED = True
        config.GROQ_API_KEY = "test-groq"
        config.GROQ_MODEL = "qwen/qwen3.8-27b"
        config.GROQ_FALLBACK_MODEL = "openai/gpt-oss-120b"
        self.fake = _FakeClient()
        enrich._gemini_client = self.fake

    def tearDown(self):
        (
            config.GEMINI_ENABLED,
            config.GROQ_ENABLED,
            config.GROQ_API_KEY,
            config.GROQ_MODEL,
            config.GROQ_FALLBACK_MODEL,
            enrich._gemini_client,
        ) = self._saved

    def test_gemini_quota_falls_over_to_groq(self):
        ans, model = aiask._generate([{"text": "CASE DATA: x"}], "sys")
        self.assertEqual(ans, "groq answer")
        self.assertTrue(model.startswith("groq:"))
        self.assertTrue(any("groq.com" in u for u in self.fake.urls))

    def test_image_question_never_touches_groq(self):
        parts = [
            {"text": "CASE DATA: x"},
            {"inline_data": {"mime_type": "image/jpeg", "data": "AAA"}},
        ]
        ans, model = aiask._generate(parts, "sys")
        self.assertIsNone(ans)
        self.assertFalse(any("groq.com" in u for u in self.fake.urls))

    def test_groq_disabled_reports_offline_path(self):
        config.GROQ_ENABLED = False
        ans, model = aiask._generate([{"text": "CASE DATA: x"}], "sys")
        self.assertIsNone(ans)
        self.assertEqual(model, "")


if __name__ == "__main__":
    unittest.main()
