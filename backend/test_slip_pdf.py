import hashlib
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from . import app as app_module, config, db, slip_pdf

WALLET = "T" + "B" * 33
FILTERS = dict(source="object", since="2026-11-16T00:00:00Z", until="2026-11-23T23:59:59Z",
               direction="both", folder="", q="", tx_status="")


@pytest.fixture
def database(monkeypatch, tmp_path):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    db._migrate(c)
    monkeypatch.setattr(db, "connect", lambda: c)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "EVIDENCE_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(app_module, "_stats_cache", {})
    yield c
    c.close()


def seed(c, key, tx="a" * 64, status="verified", etag="", direction="deposit", rail=WALLET):
    c.execute("INSERT INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext) VALUES (?,?,?,?,?,?,?)",
        (key, key.split('/')[0]+'/', 100, "2026-11-20T12:00:00Z", etag, "2026-11-20T12:01:00Z", ".jpg"))
    c.execute("INSERT INTO payment_transactions(source_key,direction,rail_type,rail_id,txid,tx_status,network,last_modified) VALUES (?,?,?,?,?,?,?,?)",
        (key, direction, "crypto_wallet", rail, tx, status, "TRON", "2026-11-20T12:00:00Z"))
    c.commit()


def png(color="white"):
    image = Image.new("RGB", (120, 240), color)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_database_hits_and_pagination_group_before_limit(database):
    seed(database, "deposit/a.jpg")
    seed(database, "deposit/b.jpg")
    seed(database, "deposit/c.jpg", tx="b" * 64)
    hit_filters = {k: v for k, v in FILTERS.items() if k != "q"}
    raw = db.money_flows_unique_hits("crypto_wallet", WALLET, **hit_filters)
    out = db.money_flows_unique_hits("crypto_wallet", WALLET, deduplicate=True, limit=1, **hit_filters)
    assert raw["rows_total"] == 3
    assert out["rows_total"] == 2 and out["duplicates_removed"] == 1
    assert len(out["items"]) == 1
    all_rows = db.crypto_deduplicated_flows(**FILTERS)
    assert all_rows["rows_total"] == 2
    assert sum(len(r["source_keys"]) for r in all_rows["items"]) == 3


def test_pdf_candidate_filters_include_image_only_wallets(database):
    seed(database, "deposit/a.jpg")
    seed(database, "withdrawal/b.jpg", tx="b" * 64, direction="withdrawal")
    database.execute("INSERT INTO wallet_hits(address,key,last_modified,tx_status) VALUES (?,?,?,?)",
        (WALLET, "withdrawal/only-image.jpg", "2026-11-20T12:00:00Z", "verified"))
    assert len(db.crypto_slip_candidates(**FILTERS)) == 3
    selected = dict(FILTERS, direction="deposit", tx_status="verified")
    assert [r["source_key"] for r in db.crypto_slip_candidates(**selected)] == ["deposit/a.jpg"]
    assert db.crypto_slip_candidates(**dict(FILTERS, tx_status="ambiguous")) == []
    assert db.crypto_slip_candidates(**dict(FILTERS, q="no-such-wallet")) == []
    assert db.crypto_slip_candidates(**dict(FILTERS, source="reference")) == []


def test_candidate_limit_does_not_silently_truncate(database):
    for i in range(3):
        seed(database, f"deposit/{i}.jpg")
    with pytest.raises(ValueError, match="narrower date range"):
        db.crypto_slip_candidates(**FILTERS, max_sources=2)


def test_fingerprint_invalidated_by_object_version(database):
    seed(database, "deposit/a.jpg", etag="old")
    db.save_slip_fingerprints([("deposit/a.jpg", "old", 100, "sha", "pixels", "now")])
    assert db.crypto_slip_candidates(**FILTERS)[0]["content_sha256"] == "sha"
    database.execute("UPDATE objects SET etag='new' WHERE key='deposit/a.jpg'")
    assert db.crypto_slip_candidates(**FILTERS)[0]["content_sha256"] == ""


def test_address_counts_use_distinct_evidence(database):
    seed(database, "deposit/a.jpg")
    seed(database, "deposit/b.jpg")
    seed(database, "deposit/c.jpg", tx="b" * 64)
    result = db.crypto_unique_deduplicated(db.money_flows_unique(rail_type="crypto_wallet", **FILTERS), **FILTERS)
    assert result["transactions"] == 2 and result["duplicates_removed"] == 1
    assert result["items"][0]["slips"] == 2
    assert result["items"][0]["duplicate_count"] == 1


def test_job_singleflight_and_queue_limit(database, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(slip_pdf.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    first = slip_pdf.start_job(FILTERS, lambda _: b"")
    assert slip_pdf.start_job(FILTERS, lambda _: b"")["id"] == first["id"]
    slip_pdf.start_job(dict(FILTERS, q="second"), lambda _: b"")
    slip_pdf.start_job(dict(FILTERS, q="third"), lambda _: b"")
    with pytest.raises(RuntimeError, match="already queued"):
        slip_pdf.start_job(dict(FILTERS, q="fourth"), lambda _: b"")


def test_empty_pack_fails_explicitly(database):
    folder = slip_pdf.job_root() / ("e" * 32)
    folder.mkdir()
    job = dict(id="e" * 32, filters=FILTERS, state="queued", processed=0, total=0)
    slip_pdf._worker(folder, job, lambda _: png())
    assert job["state"] == "failed" and "No crypto" in job["error"]
    assert not (folder / "slips.pdf").exists()


def test_large_pack_exceeds_both_old_limits_without_omission(database, monkeypatch):
    rows = [dict(source_key=f"deposit/{i}.png", txid=f"{i % 501:064x}",
                 tx_status="verified", rail_id=WALLET, network="TRON") for i in range(5511)]
    monkeypatch.setattr(db, "crypto_slip_candidates", lambda **filters: rows)
    folder = slip_pdf.job_root() / ("1" * 32)
    folder.mkdir()
    job = dict(id="1" * 32, filters=FILTERS, state="queued", processed=0, total=0)
    def fetch(key):
        n = int(key.split("/")[1].split(".")[0]) % 501
        return png((n % 256, n // 256, 40))
    slip_pdf._worker(folder, job, fetch)
    assert job["state"] == "complete", job
    assert job["source_files_total"] == 5511
    assert job["slips"] == 501 and job["duplicates_removed"] == 5010
    reader = PdfReader(folder / "slips.pdf", strict=True)
    assert len(reader.pages) == 501
    assert all(len(page.images) == 1 for page in reader.pages)


def test_render_preserves_alpha_and_cleans_partial_on_disk_failure(database, monkeypatch):
    folder = slip_pdf.job_root()
    image_path = folder / "transparent.png"
    Image.new("RGBA", (12, 24), (10, 20, 30, 100)).save(image_path)
    rows = [dict(image_path=str(image_path))]
    target = folder / "alpha.pdf"
    slip_pdf.render_pdf(target, rows, FILTERS, 1)
    reader = PdfReader(target, strict=True)
    image = next(iter(reader.pages[0]["/Resources"]["/XObject"].get_object().values())).get_object()
    assert "/SMask" in image
    assert image.get_data() == bytes((10, 20, 30)) * (12 * 24)
    assert image["/SMask"].get_data() == bytes([100]) * (12 * 24)
    monkeypatch.setattr(slip_pdf.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 0})())
    target = folder / "failed.pdf"
    with pytest.raises(ValueError, match="disk space"):
        slip_pdf.render_pdf(target, rows, FILTERS, 1)
    assert not target.exists() and not target.with_suffix(".partial").exists()


def test_heartbeat_keeps_long_jobs_alive(database):
    import time
    folder = slip_pdf.job_root() / ("2" * 32)
    folder.mkdir()
    slip_pdf._write(folder / "status.json", dict(id="2" * 32, state="running", updated=time.time() - 90000))
    with slip_pdf._heartbeat(folder):
        assert slip_pdf.read_job("2" * 32)["state"] == "running"


def test_worker_pdf_is_image_only_and_retains_provenance_server_side(database, tmp_path):
    seed(database, "deposit/a.jpg")
    seed(database, "deposit/b.jpg")
    folder = slip_pdf.job_root() / ("a" * 32)
    folder.mkdir()
    job = dict(id="a" * 32, filters=FILTERS, state="queued", processed=0, total=0)
    calls = []
    def fetch(key):
        calls.append(key)
        return png()
    slip_pdf._worker(folder, job, fetch)
    assert job["state"] == "complete", job
    assert job["slips"] == 1 and job["duplicates_removed"] == 1
    assert len(calls) == 1  # known duplicates fetched only once
    reader = PdfReader(folder / "slips.pdf")
    assert len(reader.pages) == 1
    assert len(reader.pages[0].images) == 1
    assert reader.pages[0].extract_text() == ""
    assert not reader.attachments
    assert next(folder.glob("slip-*.png")).read_bytes() == png()
    manifest = json.loads((folder / "slips.json").read_text())
    assert manifest["source_files"] == 2
    assert len(manifest["slips"][0]["source_keys"]) == 2
    assert manifest["slips"][0]["content_sha256"] == hashlib.sha256(png()).hexdigest()


def test_exact_pixels_group_unknown_transactions(database):
    seed(database, "deposit/a.jpg", tx="", status="")
    seed(database, "deposit/b.jpg", tx="", status="")
    folder = slip_pdf.job_root() / ("b" * 32)
    folder.mkdir()
    job = dict(id="b" * 32, filters=FILTERS, state="queued", processed=0, total=0)
    slip_pdf._worker(folder, job, lambda _: png())
    assert job["state"] == "complete"
    assert job["slips"] == 1 and job["duplicates_removed"] == 1
    assert db.crypto_deduplicated_flows(**FILTERS)["rows_total"] == 1


def test_missing_image_fails_without_producing_partial_pdf(database):
    seed(database, "deposit/a.jpg")
    seed(database, "deposit/b.jpg", tx="b" * 64)
    folder = slip_pdf.job_root() / ("c" * 32)
    folder.mkdir()
    job = dict(id="c" * 32, filters=FILTERS, state="queued", processed=0, total=0)
    def fetch(key):
        if key.endswith("b.jpg"):
            raise ValueError("not available")
        return png()
    slip_pdf._worker(folder, job, fetch)
    assert job["state"] == "failed" and "No incomplete PDF" in job["error"]
    assert not (folder / "slips.pdf").exists()


def test_original_jpeg_is_not_resized_or_recompressed(database):
    seed(database, "deposit/a.jpg")
    buffer = io.BytesIO()
    Image.new("RGB", (3100, 4500), "white").save(buffer, "JPEG", quality=91)
    original = buffer.getvalue()
    folder = slip_pdf.job_root() / ("f" * 32)
    folder.mkdir()
    job = dict(id="f" * 32, filters=FILTERS, state="queued", processed=0, total=0)
    slip_pdf._worker(folder, job, lambda _: original)
    assert job["state"] == "complete", job
    assert next(folder.glob("slip-*.jpg")).read_bytes() == original
    reader = PdfReader(folder / "slips.pdf")
    assert len(reader.pages) == 1 and reader.pages[0].extract_text() == ""
    assert not reader.attachments
    image = next(iter(reader.pages[0]["/Resources"]["/XObject"].get_object().values())).get_object()
    assert "/DCTDecode" in str(image["/Filter"])
    assert image["/Width"] == 3100 and image["/Height"] == 4500
    assert abs(float(reader.pages[0].mediabox.width) / float(reader.pages[0].mediabox.height) - 3100 / 4500) < 1e-6


def test_api_auth_validation_and_cross_worker_download(database, monkeypatch):
    client = TestClient(app_module.app)
    monkeypatch.setattr(config, "API_TOKEN", "test-pdf-token")
    monkeypatch.delenv("S3LM_NO_GATE", raising=False)
    assert client.post("/api/export/crypto-slips-pdf", json=FILTERS).status_code == 401
    headers = {"Authorization": "Bearer test-pdf-token"}
    assert client.post("/api/export/crypto-slips-pdf", json=dict(FILTERS, since="bad"), headers=headers).status_code == 422
    assert client.post("/api/export/crypto-slips-pdf", json=dict(FILTERS, since="2027-01-01"), headers=headers).status_code == 422
    assert client.get("/api/export/crypto-slips-pdf/not-a-job/download", headers=headers).status_code == 404
    # Disk-backed state remains readable without any originating worker globals.
    folder = slip_pdf.job_root() / ("d" * 32)
    folder.mkdir()
    slip_pdf._write(folder / "status.json", dict(id="d" * 32, state="complete", filters=FILTERS, updated=0))
    (folder / "slips.pdf").write_bytes(b"%PDF-test")
    response = client.get("/api/export/crypto-slips-pdf/" + "d" * 32 + "/download", headers=headers)
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert "attachment" in response.headers["content-disposition"]
    base = "/api/export/crypto-slips-pdf/" + "d" * 32
    assert client.post(base + "/download-access").status_code == 401
    access = client.post(base + "/download-access", headers=headers)
    assert access.status_code == 200
    cookie = access.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=" + base + "/download" in cookie
    assert client.get(access.json()["url"]).status_code == 200
    assert client.get(base).status_code == 401  # cookie cannot access other APIs
    ticket = slip_pdf.download_ticket("d" * 32)
    assert slip_pdf.valid_download_ticket("d" * 32, ticket)
    assert not slip_pdf.valid_download_ticket("a" * 32, ticket)
    assert not slip_pdf.valid_download_ticket("d" * 32, slip_pdf.download_ticket("d" * 32, 1))
    assert not slip_pdf.valid_download_ticket("d" * 32, "bad")
