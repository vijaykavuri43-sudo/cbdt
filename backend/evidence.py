"""Evidence handling: sha256-verified downloads into intelligence/ artifacts + collection-log append."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, db


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_dir() -> Path:
    d = config.DOWNLOADS_ROOT / time.strftime("%Y-%m-%d", time.gmtime())
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_collection_log(entry: dict):
    config.EVIDENCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "utc": utcnow(),
        "case": config.CASE_ID,
        "authorization_ref": config.AUTH_REF,
        **entry,
    }
    with open(config.EVIDENCE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        import subprocess

        h = subprocess.run(
            ["sha256sum", str(config.EVIDENCE_LOG)], capture_output=True, text=True
        )
        if h.returncode == 0:
            with open(str(config.EVIDENCE_LOG) + ".sha256", "w") as f:
                f.write(h.stdout)
    except Exception:
        pass


class DownloadManager:
    """Queue of keys to fetch; parallel workers; sha256 each file; manifest per batch."""

    def __init__(self, s3: Any, workers: int = 4):
        self.s3 = s3
        self.workers = workers
        self.jobs: dict[str, dict] = {}
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self._counter = 0

    def enqueue(self, keys: list[str], label: str = "") -> str:
        self._counter += 1
        job_id = (
            f"DL-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{self._counter:04d}"
        )
        job = {
            "job_id": job_id,
            "label": label,
            "total": len(keys),
            "done": 0,
            "failed": 0,
            "bytes": 0,
            "status": "queued",
            "files": [],
            "queued_utc": utcnow(),
            "outdir": str(today_dir() / job_id),
        }
        self.jobs[job_id] = job
        Path(job["outdir"]).mkdir(parents=True, exist_ok=True)

        def run():
            job["status"] = "downloading"

            def one(key: str):
                try:
                    info = self.download_one(key, Path(job["outdir"]))
                    job["done"] += 1
                    job["bytes"] += info["bytes"]
                    job["files"].append(info)
                except Exception as e:
                    job["failed"] += 1
                    job["files"].append({"key": key, "error": str(e)})
                if job["done"] + job["failed"] >= job["total"]:
                    job["status"] = "complete"
                    self._finalize(job)

            futures = [self.executor.submit(one, k) for k in keys]
            for f in futures:
                f.result()

        self.executor.submit(run)
        return job_id

    def download_one(self, key: str, outdir: Path) -> dict:
        dest = outdir / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        h = hashlib.sha256()
        n = 0
        resp, headers = self.s3.get_object_stream(key)
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes(65536):
                f.write(chunk)
                h.update(chunk)
                n += len(chunk)
        resp.close()
        tmp.rename(dest)
        rec = {
            "key": key,
            "path": str(dest),
            "bytes": n,
            "sha256": h.hexdigest(),
            "source_etag": headers.get("etag", ""),
            "last_modified": headers.get("last_modified", ""),
            "fetched_utc": utcnow(),
        }
        append_collection_log(
            {
                "event": "evidence_download",
                "method": f"authenticated object GET from {config.SOURCE_ID}",
                "artifact_path": str(dest),
                "artifact_sha256": rec["sha256"],
                "source_key": key,
            }
        )
        return rec

    def _finalize(self, job: dict):
        man = {
            "job_id": job["job_id"],
            "label": job["label"],
            "case": config.CASE_ID,
            "authorization_ref": config.AUTH_REF,
            "bucket": config.BUCKET_URL,
            "queued_utc": job["queued_utc"],
            "finished_utc": utcnow(),
            "counts": {
                "total": job["total"],
                "ok": job["done"],
                "failed": job["failed"],
            },
            "files": job["files"],
        }
        p = Path(job["outdir"]) / "_MANIFEST.json"
        p.write_text(json.dumps(man, indent=2, ensure_ascii=False))
        append_collection_log(
            {
                "event": "evidence_batch_complete",
                "method": f"bulk object GET from {config.SOURCE_ID}",
                "artifact_path": job["outdir"],
                "manifest_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "counts": man["counts"],
            }
        )


def load_master_vpas() -> set[str]:
    try:
        txt = config.MASTER_UPI_LIST.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    vpas = set()
    for line in txt.splitlines():
        for tok in line.replace(",", " ").replace(";", " ").split():
            if "@" in tok and 3 <= len(tok) <= 80:
                vpas.add(tok.strip().lower())
    return vpas


def _master_wallet_csv_header() -> tuple[str, list[str]]:
    try:
        rel = config.MASTER_WALLET_CSV.relative_to(config.REPO_ROOT)
    except Exception:
        rel = config.MASTER_WALLET_CSV
    return (str(rel), [])


def load_master_wallet_rows() -> list[dict]:
    """Full master rows with provenance (entity_id, network, address, label, case_ids, notes)."""
    import csv as _csv

    rows: list[dict] = []
    try:
        with open(
            config.MASTER_WALLET_CSV, encoding="utf-8", errors="ignore", newline=""
        ) as f:
            reader = _csv.DictReader(f)
            if not reader.fieldnames or "address" not in reader.fieldnames:
                return rows
            for r in reader:
                addr = (r.get("address") or "").strip()
                if not addr:
                    continue
                rows.append(
                    {
                        "entity_id": (r.get("entity_id") or "").strip(),
                        "network": (r.get("network") or "").strip() or "TRON",
                        "address": addr.strip(),
                        "address_lower": addr.strip().lower(),
                        "label": (r.get("label") or "").strip(),
                        "case_ids": (r.get("case_ids") or "").strip(),
                        "confidence": (r.get("confidence") or "").strip(),
                        "fact_type": (r.get("fact_type") or "").strip(),
                        "notes": (r.get("notes") or "").strip(),
                    }
                )
    except OSError:
        pass
    return rows


def load_reference_wallets() -> list[dict]:
    """All previously-identified wallets (canonical reference census).

    Source: s3-live-monitor/reference/identified-wallets.csv (built by
    scripts/build-identified-wallets.py from 36-ALL-IDENTIFIED-WALLETS.csv +
    WALLET-PROOF-MAP + ALL-WALLETS-MASTER + roll-data extras, minus the
    28 base58-invalid/non-address rows quarantined 2026-09-17/2026-10-25 =
    87 rows; 5 evidence-only additions from the 2026-09-21 CBTF wallet
    document = 92).
    These are report attributions with their original proof references —
    shown in the Crypto tab alongside live bucket decodes so no previously
    identified wallet ever goes missing from the dashboard.
    """
    import csv as _csv
    import os as _os

    cands = []
    env = _os.environ.get("S3LM_REFERENCE_WALLETS_CSV", "")
    if env:
        cands.append(Path(env))
    cands.append(config.APP_DIR / "reference" / "identified-wallets.csv")
    try:
        cands.append(config.DATA_DIR / "reference" / "identified-wallets.csv")
    except Exception:
        pass
    rows: list[dict] = []
    found_file = ""
    for cand in cands:
        try:
            if not cand.is_file():
                continue
        except OSError:
            continue
        try:
            with open(cand, encoding="utf-8", errors="ignore", newline="") as f:
                reader = _csv.DictReader(f)
                if not reader.fieldnames or "address" not in reader.fieldnames:
                    continue
                for r in reader:
                    addr = (r.get("address") or "").strip()
                    if not addr:
                        continue
                    rows.append(
                        {k: (r.get(k) or "").strip() for k in reader.fieldnames}
                        | {"address": addr}
                    )
        except OSError:
            continue
        found_file = str(cand)
        break
    if not found_file:
        print(
            "[wallets] WARNING: identified-wallets.csv not found in "
            f"{[str(c) for c in cands]} — reference section will be empty",
            flush=True,
        )
    return rows


def load_master_wallets() -> set[str]:
    """Known operator/hawala crypto wallets from the LE submission master CSV.

    Preferred schema: entity_id,network,address,label,... — reads the `address`
    column; falls back to scanning any cell that looks address-shaped.
    """
    import csv as _csv

    rows = load_master_wallet_rows()
    if rows:
        return {r["address_lower"] for r in rows}
    wallets: set[str] = set()
    try:
        # fallback: shape-scan every cell of a headerless file
        with open(
            config.MASTER_WALLET_CSV, encoding="utf-8", errors="ignore", newline=""
        ) as f:
            for row in _csv.reader(f):
                for cell in row:
                    c = cell.strip().lower()
                    if (
                        26 <= len(c) <= 90
                        and "@" not in c
                        and ":" not in c
                        and "." not in c
                    ):
                        wallets.add(c)
    except OSError:
        pass
    return wallets
