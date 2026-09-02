"""GCS-backed object client exposing the small read-only interface used by the app."""

from __future__ import annotations

from datetime import timezone
from typing import Iterator, Optional

from google.cloud import storage

from .db import normalize_object_ts


class _BlobStream:
    def __init__(self, handle):
        self._handle = handle

    def read(self, size: int = -1) -> bytes:
        return self._handle.read(size)

    def iter_bytes(self, chunk_size: int = 65536):
        while True:
            chunk = self._handle.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self):
        self._handle.close()


class GCSMirrorClient:
    """Read/list a private GCS evidence mirror while presenting original keys."""

    def __init__(self, bucket_name: str, mirror_prefix: str = "s3-mirror/"):
        self.bucket_name = bucket_name
        self.mirror_prefix = mirror_prefix.strip("/") + "/"
        self.client = storage.Client()
        self.bucket = self.client.bucket(bucket_name)
        self.source_id = f"gs://{bucket_name}/{self.mirror_prefix}"
        self.lister_errors = 0

    def _stored(self, key: str) -> str:
        return self.mirror_prefix + key

    def _original(self, stored: str) -> str:
        return stored[len(self.mirror_prefix) :]

    def list_page(
        self,
        prefix: str = "",
        continuation_token: Optional[str] = None,
        delimiter: Optional[str] = None,
        start_after: Optional[str] = None,
        max_keys: int = 1000,
    ) -> dict:
        stored_prefix = self._stored(prefix)
        kwargs = {
            "prefix": stored_prefix,
            "delimiter": delimiter,
            "max_results": max_keys,
            "page_token": continuation_token or None,
        }
        if start_after:
            # GCS start_offset is inclusive; filter the watermark itself below.
            kwargs["start_offset"] = self._stored(start_after)
        iterator = self.client.list_blobs(self.bucket_name, **kwargs)
        try:
            page = next(iterator.pages)
        except StopIteration:
            return {
                "objects": [], "prefixes": [], "is_truncated": False,
                "next_token": None, "key_count": 0,
            }
        objects = []
        for blob in page:
            key = self._original(blob.name)
            if start_after and key <= start_after:
                continue
            metadata = blob.metadata or {}
            # Mirror-sync stores the original S3 LastModified in custom
            # metadata — historically as an RFC-1123 HTTP-date, which corrupts
            # lexicographic -modified sorting if stored verbatim. Normalize
            # every value to ISO-8601 UTC before it reaches the manifest.
            modified = normalize_object_ts(metadata.get("last_modified"))
            if not modified and blob.updated:
                modified = blob.updated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            objects.append(
                {
                    "key": key,
                    "size": int(blob.size or 0),
                    "last_modified": modified or "",
                    "etag": metadata.get("s3_etag") or blob.etag or "",
                    "storage_class": blob.storage_class or "STANDARD",
                }
            )
        prefixes = []
        for stored in iterator.prefixes:
            if stored.startswith(self.mirror_prefix):
                original = self._original(stored)
                if original:
                    prefixes.append(original)
        next_token = iterator.next_page_token
        return {
            "objects": objects,
            "prefixes": sorted(set(prefixes)),
            "is_truncated": bool(next_token),
            "next_token": next_token,
            "key_count": len(objects),
        }

    def iter_all(self, prefix: str = "") -> Iterator[dict]:
        token = None
        while True:
            page = self.list_page(prefix, continuation_token=token)
            yield from page["objects"]
            token = page["next_token"]
            if not token:
                return

    def list_prefixes_top(self) -> list[str]:
        page = self.list_page("", delimiter="/", max_keys=1000)
        return sorted(p for p in page["prefixes"] if p)

    def head_bucket_ok(self) -> bool:
        try:
            self.list_page("", max_keys=1)
            return True
        except Exception:
            self.lister_errors += 1
            return False

    def get_object_stream(self, key: str, chunk_size: int = 65536):
        blob = self.bucket.get_blob(self._stored(key))
        if blob is None:
            raise RuntimeError(f"GetObject {key} -> not found in GCS mirror")
        metadata = blob.metadata or {}
        handle = blob.open("rb", chunk_size=max(256 * 1024, chunk_size))
        headers = {
            "content_type": blob.content_type or "application/octet-stream",
            "content_length": str(blob.size or 0),
            "etag": metadata.get("s3_etag") or blob.etag or "",
            "last_modified": normalize_object_ts(metadata.get("last_modified"))
            or (
                blob.updated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if blob.updated else ""
            ),
            "sha256": metadata.get("sha256") or "",
        }
        return _BlobStream(handle), headers
