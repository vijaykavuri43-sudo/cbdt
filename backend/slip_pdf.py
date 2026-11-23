"""Authenticated, disk-backed PDF jobs shared by all API workers.

Jobs are bounded, singleflight by filters, and serialized per data volume.
Original evidence is never changed. Unknown/unavailable sources are disclosed.
"""

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from . import config, db
from .slip_dedup import deduplicate_slips

MAX_SOURCES = 5000
MAX_SLIPS = 500
MAX_IMAGE_BYTES = 25 * 1024 * 1024


def job_root():
    root = config.DATA_DIR / "officer-slip-pdf"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _write(path, value):
    temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=True))
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def read_job(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise FileNotFoundError("PDF job not found")
    path = job_root() / job_id / "status.json"
    if not path.exists():
        raise FileNotFoundError("PDF job not found")
    status = json.loads(path.read_text())
    if status["state"] in ("queued", "running") and time.time() - status["updated"] > 1800:
        status.update(state="failed", error="PDF preparation was interrupted. Please prepare it again.")
    return status


def start_job(filters, fetch_bytes):
    root = job_root()
    digest = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    with (root / "queue.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        active = 0
        for path in root.glob("*/status.json"):
            try:
                job = read_job(path.parent.name)
            except (ValueError, FileNotFoundError):
                continue
            if time.time() - job["updated"] > 86400:
                # Only derived, UUID-named job directories, never source evidence.
                shutil.rmtree(path.parent)
                continue
            if job["state"] in ("queued", "running"):
                active += 1
            if job.get("filter_digest") == digest and job["state"] in ("queued", "running"):
                return job
        if active >= 3:
            raise RuntimeError("Three PDF jobs are already queued. Please wait for one to finish.")
        identity = uuid.uuid4().hex
        folder = root / identity
        folder.mkdir(mode=0o700)
        job = dict(id=identity, state="queued", filters=filters, filter_digest=digest,
                   updated=time.time(), processed=0, total=0)
        _write(folder / "status.json", job)
        response = dict(job)
        threading.Thread(target=_worker, args=(folder, job, fetch_bytes), daemon=True,
                         name="officer-pdf-" + identity[:8]).start()
        return response


def pixel_fingerprint(data):
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Source image exceeds the 25 MB PDF safety limit")
    with Image.open(io.BytesIO(data)) as source:
        if getattr(source, "n_frames", 1) > 1:
            raise ValueError("Multi-frame image: use the original source file for all frames")
        if source.width * source.height > 40_000_000:
            raise ValueError("Source image exceeds the 40-megapixel PDF safety limit")
        image = ImageOps.exif_transpose(source).convert("RGB")
        digest = hashlib.sha256(f"{image.width}x{image.height}:RGB:".encode() + image.tobytes()).hexdigest()
        return image, digest


def _worker(folder, job, fetch_bytes):
    status = folder / "status.json"
    fingerprints = []
    try:
        # A file lock rather than an in-memory semaphore covers both uvicorn
        # processes and protects the live query workload from parallel packs.
        with (job_root() / "render.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            job.update(state="running", updated=time.time())
            _write(status, job)
            raw = db.crypto_slip_candidates(**job["filters"], max_sources=MAX_SOURCES)
            if not raw:
                raise ValueError("No crypto source images match the selected filters.")
            by_key = {r["source_key"]: r for r in raw}
            grouped = deduplicate_slips(raw)
            if len(grouped) > MAX_SLIPS:
                raise ValueError(f"More than {MAX_SLIPS} distinct slips match. Select a narrower date range; no files were omitted.")
            job.update(total=len(grouped), source_files_total=len(raw), updated=time.time())
            _write(status, job)
            loaded = []
            for index, row in enumerate(grouped):
                error = "Source unavailable"
                for key in [row["source_key"]] + [k for k in row["source_keys"] if k != row["source_key"]]:
                    try:
                        data = fetch_bytes(key)
                        image, pixels = pixel_fingerprint(data)
                        sha = hashlib.sha256(data).hexdigest()
                        meta = by_key[key]
                        fingerprints.append((key, meta.get("object_etag") or "", meta.get("object_size") or len(data),
                                             sha, pixels, datetime.now(timezone.utc).isoformat()))
                        # Keep JPEG/PNG originals byte-for-byte: no thumbnail,
                        # resampling, recompression, crop or added annotation.
                        with Image.open(io.BytesIO(data)) as source:
                            original_format = source.format
                            orientation = source.getexif().get(274, 1)
                        if original_format in ("JPEG", "PNG") and orientation == 1:
                            path = folder / f"slip-{index}.{'jpg' if original_format == 'JPEG' else 'png'}"
                            path.write_bytes(data)
                        else:
                            path = folder / f"slip-{index}.png"
                            image.save(path, "PNG")  # lossless format/orientation normalization
                        row.update(source_key=key, image_path=str(path), content_sha256=sha, pixel_sha256=pixels)
                        error = ""
                        break
                    except Exception as exc:
                        error = type(exc).__name__ + ": " + str(exc)[:180]
                if error:
                    row["image_error"] = error
                loaded.append(row)
                job.update(processed=index + 1, updated=time.time())
                _write(status, job)
            # Exact decoded-pixel/byte identities catch copies that differ in
            # encoding or metadata; never use fuzzy image similarity.
            final = deduplicate_slips(loaded)
            image_paths = {r["source_key"]: r for r in loaded if r.get("image_path")}
            for row in final:
                if not row.get("image_path"):
                    available = next((image_paths[k] for k in row["source_keys"] if k in image_paths), None)
                    if available:
                        row.update(image_path=available["image_path"], content_sha256=available["content_sha256"],
                                   source_key=available["source_key"])
                        row.pop("image_error", None)
            missing = sum(not r.get("image_path") for r in final)
            if missing:
                raise ValueError(f"{missing} matching slip image(s) could not be read. No incomplete PDF was produced. Check source availability and retry.")
            db.save_slip_fingerprints(fingerprints)
            render_pdf(folder / "slips.pdf", final, job["filters"], len(raw))
            from .evidence import append_collection_log
            output_sha = hashlib.sha256((folder / "slips.pdf").read_bytes()).hexdigest()
            append_collection_log(dict(event="officer_deduplicated_crypto_pdf", method="filtered source images and conservative deduplication",
                artifact_path=str(folder / "slips.pdf"), artifact_sha256=output_sha,
                source_files=len(raw), slips=len(final), missing=missing, filters=job["filters"]))
            job.update(state="complete", slips=len(final), missing=missing,
                       duplicates_removed=len(raw) - len(final), sha256=output_sha, updated=time.time())
            _write(status, job)
    except Exception as exc:
        job.update(state="failed", error=str(exc)[:350], updated=time.time())
        _write(status, job)


def render_pdf(path, rows, filters, original_count):
    from reportlab.pdfgen import canvas

    if not rows or any(not row.get("image_path") for row in rows):
        raise ValueError("Every slip must have an image; no incomplete PDF was produced")
    pdf = canvas.Canvas(str(path), pageCompression=1)
    for row in rows:
        with Image.open(row["image_path"]) as image:
            # Match the original aspect ratio exactly, edge-to-edge, retaining
            # its full pixel dimensions in the embedded image.
            scale = 595.0 / max(image.width, image.height)
            width, height = image.width * scale, image.height * scale
        pdf.setPageSize((width, height))
        pdf.drawImage(row["image_path"], 0, 0, width=width, height=height, mask="auto")
        pdf.showPage()
    pdf.save()
    # Provenance stays server-side only; nothing is attached to the image-only PDF.
    manifest = dict(filters=filters, source=config.SOURCE_LABEL, source_files=original_count,
                    slips=[{k: v for k, v in row.items() if k != "image_path"} for row in rows])
    _write(path.with_suffix(".json"), manifest)
