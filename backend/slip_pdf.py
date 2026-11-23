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
                        path = folder / f"slip-{index}.jpg"
                        image.thumbnail((2800, 2800))
                        image.save(path, "JPEG", quality=93)
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
            if missing == len(final):
                raise ValueError("None of the matching source images could be read. Check source availability and retry.")
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
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    pdf.setTitle("Deduplicated crypto source slips")
    pdf.setAuthor("Evidence Monitor")
    width, height = A4
    margin = 32

    def text_lines(text, y, size=9):
        pdf.setFont("Helvetica", size)
        # Built-in PDF fonts do not cover arbitrary uploader filenames.
        # Preserve full Unicode paths separately in the attached JSON manifest.
        text = str(text).encode("cp1252", "replace").decode("cp1252")
        lines = simpleSplit(text, "Helvetica", size, width - margin * 2)
        wrapped = []
        for line in lines:
            while stringWidth(line, "Helvetica", size) > width - margin * 2:
                end = max(1, len(line) // 2)
                while stringWidth(line[:end], "Helvetica", size) > width - margin * 2:
                    end -= 1
                wrapped.append(line[:end])
                line = line[end:]
            wrapped.append(line)
        for line in wrapped:
            if y < 30:
                pdf.showPage()
                y = height - 35
                pdf.setFont("Helvetica", size)
            pdf.drawString(margin, y, line)
            y -= size + 3
        return y

    y = text_lines("DEDUPLICATED CRYPTO SOURCE SLIPS", height - 40, 17)
    y -= 18
    for line in (
        "Source: " + config.SOURCE_LABEL,
        "UTC range: " + filters["since"] + " to " + filters["until"],
        "Folder: " + (filters.get("folder") or "all") + " | Side: " + filters.get("direction", "both"),
        "Chain status: " + (filters.get("tx_status") or "all") + " | Search: " + (filters.get("q") or "none"),
        f"Source files: {original_count} | Deduplicated slips: {len(rows)} | Copies grouped: {original_count - len(rows)}",
        "Generated UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        f"Unavailable images (explicitly listed): {sum(not r.get('image_path') for r in rows)}",
        "Grouping: verified/derived transaction hash + chain, exact source content or exact decoded pixels.",
        "No grouping by wallet alone, amount alone, username or approximate visual similarity.",
        "Original files are preserved. Each slip has a source-key appendix listing grouped copies.",
        "Images are fitted to PDF pages. The embedded source-manifest.json preserves full original paths and hashes.",
        "A transaction may have multiple transfers; this pack shows one representative source image per grouped transaction.",
        "These are source images, not proof of operator ownership or criminal attribution.",
    ):
        y = text_lines(line, y - 10)
    pdf.showPage()
    for index, row in enumerate(rows, 1):
        y = text_lines(f"Slip {index} of {len(rows)} | {len(row['source_keys'])} source file(s)", height - 30, 11)
        wallets = row.get("rail_ids") or [row.get("rail_id") or "unknown"]
        y = text_lines("Wallet(s): " + ", ".join(wallets[:3]) + (" (see manifest for more)" if len(wallets) > 3 else ""), y - 3, 8)
        y = text_lines("Transaction: " + (row.get("txid") or "not resolved") + " | Status: " + (row.get("tx_status") or "unchecked"), y - 3, 8)
        y = text_lines("Source: " + row["source_key"], y - 3, 8)
        y = text_lines("Original SHA-256: " + (row.get("content_sha256") or "unavailable"), y - 3, 7)
        if row.get("image_path"):
            with Image.open(row["image_path"]) as image:
                max_w, max_h = width - margin * 2, max(100, y - 45)
                scale = min(max_w / image.width, max_h / image.height)
                draw_w, draw_h = image.width * scale, image.height * scale
                # Pass the JPEG filename so ReportLab embeds its compressed
                # DCT stream. Passing a PIL object expands it to RGB/Flate and
                # made a 163-slip live pack unnecessarily exceed 200 MB.
                pdf.drawImage(row["image_path"], (width - draw_w) / 2, 35 + (max_h - draw_h) / 2,
                              width=draw_w, height=draw_h)
        else:
            text_lines("IMAGE UNAVAILABLE: " + row.get("image_error", "Unknown error"), y - 30, 11)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(margin, 17, "Original evidence retained. See source-key appendix for grouped copies.")
        pdf.showPage()
    y = text_lines("SOURCE-KEY APPENDIX", height - 35, 14)
    for index, row in enumerate(rows, 1):
        identities = row.get("group_transaction_ids", [])
        values = [f"Slip {index} | grouped copies: {row['duplicate_count']} | " + ", ".join(row.get("dedup_reasons", []))]
        if len(identities) > 1:
            values += ["WARNING: source metadata contains multiple transaction identities; all preserved in manifest."] + identities
        for value in values + row["source_keys"]:
            if y < 65:
                pdf.showPage()
                y = height - 35
            y = text_lines(value, y - 6, 8)
    pdf.save()
    # Machine-readable sidecar retains every original path, including Unicode.
    manifest = dict(filters=filters, source=config.SOURCE_LABEL, source_files=original_count,
                    slips=[{k: v for k, v in row.items() if k != "image_path"} for row in rows])
    _write(path.with_suffix(".json"), manifest)
    from pypdf import PdfWriter
    writer = PdfWriter(clone_from=str(path))
    writer.add_attachment("source-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    attached = path.with_suffix(".attached.pdf")
    with attached.open("wb") as output:
        writer.write(output)
    writer.close()
    os.replace(attached, path)
