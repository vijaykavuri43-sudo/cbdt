"""Minimal S3 REST client (read-only) for public buckets: ListObjectsV2 + GetObject."""
import fnmatch
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterator, Optional

import httpx

XMLNS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3PublicClient:
    def __init__(self, bucket_url: str, max_rate_per_sec: float = 5.0, timeout: float = 30.0):
        self.bucket_url = bucket_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)
        self._min_interval = 1.0 / max(0.1, max_rate_per_sec)
        self._last_get = 0.0
        self.lister_errors = 0

    # ---- rate limiting (applies to GetObject only; listing is light) ----
    def _throttle(self):
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_get)
        if wait > 0:
            time.sleep(wait)
        self._last_get = time.monotonic()

    # ---- ListObjectsV2 ----
    def list_page(
        self,
        prefix: str = "",
        continuation_token: Optional[str] = None,
        delimiter: Optional[str] = None,
        start_after: Optional[str] = None,
        max_keys: int = 1000,
    ) -> dict:
        params = {"list-type": "2", "max-keys": str(max_keys)}
        if prefix:
            params["prefix"] = prefix
        if delimiter:
            params["delimiter"] = delimiter
        if start_after:
            params["start-after"] = start_after
        if continuation_token:
            params["continuation-token"] = continuation_token
        r = self.client.get(self.bucket_url + "/", params=params)
        r.raise_for_status()
        return self.parse_list_xml(r.text)

    @staticmethod
    def parse_list_xml(xml_text: str) -> dict:
        root = ET.fromstring(xml_text)
        objects = []
        for c in root.iter(f"{XMLNS}Contents"):
            key = c.findtext(f"{XMLNS}Key")
            size = int(c.findtext(f"{XMLNS}Size") or "0")
            lm = c.findtext(f"{XMLNS}LastModified")
            etag = (c.findtext(f"{XMLNS}ETag") or "").strip('"')
            objects.append(
                {
                    "key": key,
                    "size": size,
                    "last_modified": lm,
                    "etag": etag,
                    "storage_class": c.findtext(f"{XMLNS}StorageClass") or "STANDARD",
                }
            )
        prefixes = [
            (cp.findtext(f"{XMLNS}Prefix") or "")
            for cp in root.iter(f"{XMLNS}CommonPrefixes")
        ]
        prefixes = [p for p in prefixes if p]
        is_truncated = (root.findtext(f"{XMLNS}IsTruncated") or "false").lower() == "true"
        next_token = root.findtext(f"{XMLNS}NextContinuationToken")
        return {
            "objects": objects,
            "prefixes": prefixes,
            "is_truncated": is_truncated,
            "next_token": next_token,
            "key_count": int(root.findtext(f"{XMLNS}KeyCount") or "0"),
        }

    def iter_all(self, prefix: str = "") -> Iterator[dict]:
        token = None
        while True:
            page = self.list_page(prefix, continuation_token=token)
            yield from page["objects"]
            if not page["is_truncated"] or not page["next_token"]:
                return
            token = page["next_token"]

    def list_prefixes_top(self) -> list[str]:
        page = self.list_page(prefix="", delimiter="/", max_keys=1000)
        return sorted(p for p in page["prefixes"] if p)

    def head_bucket_ok(self) -> bool:
        try:
            r = self.client.get(self.bucket_url + "/", params={"list-type": "2", "max-keys": "1"})
            return r.status_code == 200
        except Exception:
            return False

    # ---- GetObject (streaming) ----
    def get_object_stream(self, key: str, chunk_size: int = 65536):
        self._throttle()
        url = f"{self.bucket_url}/{quote_key(key)}"
        req = self.client.build_request("GET", url)
        resp = self.client.send(req, stream=True)
        if resp.status_code != 200:
            resp.close()
            raise RuntimeError(f"GetObject {key} -> HTTP {resp.status_code}")
        headers = {
            "content_type": resp.headers.get("content-type", "application/octet-stream"),
            "content_length": resp.headers.get("content-length"),
            "etag": (resp.headers.get("etag") or "").strip('"'),
            "last_modified": resp.headers.get("last-modified"),
        }
        return resp, headers


def quote_key(key: str) -> str:
    from urllib.parse import quote

    return quote(key, safe="/-._~")


def parse_s3_ts(ts: Optional[str]) -> str:
    """'2026-08-18T05:36:34.000Z' -> '2026-08-18T05:36:34Z'"""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ts


def ext_of(key: str) -> str:
    k = key.rsplit("/", 1)[-1]
    if "." in k:
        return "." + k.rsplit(".", 1)[1].lower()
    return ""


def match_any(key: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(key.lower(), p.lower()) for p in patterns)
