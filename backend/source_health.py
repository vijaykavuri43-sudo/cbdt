"""Distinguish upstream S3 access from preserved GCS mirror availability."""

import time

import httpx

from . import config


def probe_source():
    started = time.monotonic()
    result = dict(bucket_url=config.BUCKET_URL, source_label=config.SOURCE_LABEL,
                  checked_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  accessible=False, forbidden=False, http_status=None)
    try:
        # No object content, credentials, writes or retries. A successful GCS
        # probe must never conceal that the original S3 source denies listing.
        with httpx.Client(timeout=8.0, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", config.BUCKET_URL,
                params={"list-type": "2", "max-keys": "1", "delimiter": "/"}) as response:
                code = response.status_code
        result.update(http_status=code, accessible=code == 200, forbidden=code == 403)
        if code == 403:
            result.update(state="forbidden", message="UPSTREAM S3 FORBIDDEN (403) — fresh collection is blocked. Preserved mirror evidence remains available.")
        elif code == 200:
            result.update(state="accessible", message="Upstream S3 listing is accessible. Sync progress is reported separately.")
        else:
            result.update(state="unavailable", message=f"Upstream S3 returned HTTP {code}. Fresh collection may be interrupted; preserved evidence remains available.")
    except httpx.HTTPError:
        result.update(state="unknown", message="Upstream S3 could not be reached. Preserved mirror connectivity is checked separately.")
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result
