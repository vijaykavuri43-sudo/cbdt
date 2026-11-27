"""Authenticated, disk-backed PDF jobs shared by all API workers.

Jobs are bounded, singleflight by filters, and serialized per data volume.
Original evidence is never changed. Unknown/unavailable sources are disclosed.
"""

import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from . import config, db
from .slip_dedup import deduplicate_slips

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MIN_FREE_BYTES = 128 * 1024 * 1024


def download_ticket(job_id, expires=None):
    expires = int(time.time()) + 600 if expires is None else expires
    message = f"slip-pdf:{job_id}:{expires}"
    signature = hmac.new(config.API_TOKEN.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def valid_download_ticket(job_id, ticket):
    try:
        expires = int(ticket.split(".")[0])
        return time.time() < expires <= time.time() + 600 and hmac.compare_digest(ticket, download_ticket(job_id, expires))
    except (ValueError, AttributeError):
        return False


def _check_space(folder, needed=0):
    if shutil.disk_usage(folder).free < MIN_FREE_BYTES + needed:
        raise ValueError("Insufficient PDF workspace disk space. Free space or increase the data disk and retry; no incomplete PDF was produced.")


@contextmanager
def _heartbeat(folder):
    """Keep queued, fetching and rendering jobs alive without racing status writes."""
    stopped = threading.Event()
    marker = folder / "heartbeat"
    marker.touch()

    def beat():
        while not stopped.wait(10):
            marker.touch()

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


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
    marker = path.parent / "heartbeat"
    updated = max(status["updated"], marker.stat().st_mtime if marker.exists() else 0)
    if status["state"] in ("queued", "running") and time.time() - updated > 1800:
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
            if job["state"] not in ("queued", "running") and time.time() - job["updated"] > 86400:
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
        with _heartbeat(folder), (job_root() / "render.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            job.update(state="running", updated=time.time())
            _write(status, job)
            _check_space(folder)
            raw = db.crypto_slip_candidates(**job["filters"])
            if not raw:
                raise ValueError("No crypto source images match the selected filters.")
            by_key = {r["source_key"]: r for r in raw}
            grouped = deduplicate_slips(raw)
            job.update(total=len(grouped), source_files_total=len(raw), updated=time.time())
            _write(status, job)
            loaded = []
            for index, row in enumerate(grouped):
                error = "Source unavailable"
                for key in [row["source_key"]] + [k for k in row["source_keys"] if k != row["source_key"]]:
                    try:
                        data = fetch_bytes(key)
                        _check_space(folder, len(data) * 2)
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
                        image.close()
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
            job.update(phase="rendering", updated=time.time())
            _write(status, job)
            render_pdf(folder / "slips.pdf", final, job["filters"], len(raw))
            from .evidence import append_collection_log
            with (folder / "slips.pdf").open("rb") as output:
                output_sha = hashlib.file_digest(output, "sha256").hexdigest()
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
    """Stream PDF objects to disk; never retain the entire image pack in RAM.

    JPEG streams are embedded unchanged. Other formats use lossless RGB/alpha
    streams at their original dimensions. Only object offsets grow with pages.
    """
    if not rows or any(not row.get("image_path") for row in rows):
        raise ValueError("Every slip must have an image; no incomplete PDF was produced")
    partial = path.with_suffix(".partial")
    offsets = [0]
    pages = []
    try:
        with partial.open("wb") as output:
            output.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")

            def obj(body, stream=None):
                number = len(offsets)
                offsets.append(output.tell())
                output.write(f"{number} 0 obj\n".encode())
                if stream is None:
                    output.write(body.encode())
                else:
                    _check_space(path.parent, len(stream))
                    output.write(f"<< {body} /Length {len(stream)} >>\nstream\n".encode())
                    output.write(stream)
                    output.write(b"\nendstream")
                output.write(b"\nendobj\n")
                return number

            obj("<< /Type /Catalog /Pages 2 0 R >>")
            offsets.append(0)  # Pages root is written after its children.
            for row in rows:
                _check_space(path.parent)
                with Image.open(row["image_path"]) as image:
                    w, h = image.size
                    alpha = None
                    if image.format == "JPEG" and image.mode in ("RGB", "L"):
                        data = Path(row["image_path"]).read_bytes()
                        color = "/DeviceGray" if image.mode == "L" else "/DeviceRGB"
                        encoding = "/DCTDecode"
                    else:
                        if "A" in image.getbands() or "transparency" in image.info:
                            with image.convert("RGBA") as rgba:
                                with rgba.getchannel("A") as channel:
                                    alpha = zlib.compress(channel.tobytes())
                        with image.convert("RGB") as rgb:
                            data = zlib.compress(rgb.tobytes())
                        color, encoding = "/DeviceRGB", "/FlateDecode"
                    mask = ""
                    if alpha is not None:
                        mask_id = obj(f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode", alpha)
                        mask = f" /SMask {mask_id} 0 R"
                    image_id = obj(f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {color} /BitsPerComponent 8 /Filter {encoding}{mask}", data)
                    del data, alpha
                scale = 595.0 / max(w, h)
                width, height = w * scale, h * scale
                content = obj("", f"q {width:.8f} 0 0 {height:.8f} 0 0 cm /Im0 Do Q".encode())
                pages.append(obj(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:.8f} {height:.8f}] /Resources << /XObject << /Im0 {image_id} 0 R >> >> /Contents {content} 0 R >>"))
            offsets[2] = output.tell()
            kids = " ".join(f"{page} 0 R" for page in pages)
            output.write(f"2 0 obj\n<< /Type /Pages /Count {len(pages)} /Kids [{kids}] >>\nendobj\n".encode())
            xref = output.tell()
            output.write(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
            for offset in offsets[1:]:
                output.write(f"{offset:010d} 00000 n \n".encode())
            output.write(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    # Provenance stays server-side only; nothing is attached to the image-only PDF.
    manifest = dict(filters=filters, source=config.SOURCE_LABEL, source_files=original_count,
                    slips=[{k: v for k, v in row.items() if k != "image_path"} for row in rows])
    _write(path.with_suffix(".json"), manifest)
