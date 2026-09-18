#!/usr/bin/env python3
"""Pixel re-OCR of deposit/withdrawal slips with unproven amounts.

Does not rewrite rows whose stored rupees already match a proven parse.
Fetches the image from the GCS evidence mirror, runs ocr_slip, and keeps
the new amount only when it is proven-better (null/debris/ref-id/₹-eaten).

Run inside the s3lm container after deploy:
  python3 /app/scripts/reocr-suspect-amounts.py
  python3 /app/scripts/reocr-suspect-amounts.py --limit 500
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, db  # noqa: E402
from backend.enrich import (  # noqa: E402
    amount_is_ref_id,
    classify,
    ocr_slip,
    proven_amount_correction,
)
from backend.gcsclient import GCSMirrorClient  # noqa: E402

BATCH = 40
WORKERS = 4
EXHIBITS = (
    "deposit/1789721443450.jpeg",
    "deposit/1789719398070.jpg",
)


def _is_suspect(p: dict) -> bool:
    text = p.get("text_sample") or ""
    cur = p.get("amount_inr")
    utr = str(p.get("utr") or "")
    if cur is None:
        return True
    try:
        cf = float(cur)
    except (TypeError, ValueError):
        return True
    if cf < 100:
        return True
    if amount_is_ref_id(text, cf, utr):
        return True
    new, reason = proven_amount_correction(text, cf, utr, p.get("amount_tesseract_raw"))
    return bool(reason)


def _better(old: dict, payload: dict) -> dict | None:
    """Merge pixel OCR into stored extract; None = do not write."""
    text = payload.get("text_sample") or old.get("text_sample") or ""
    utr = str(payload.get("utr") or old.get("utr") or "")
    old_amt = old.get("amount_inr")
    new_amt = payload.get("amount_inr")
    if new_amt is not None and amount_is_ref_id(text, float(new_amt), utr):
        new_amt = None
        payload["amount_inr"] = None
        payload["amount_value"] = None
        payload["ref_as_amount"] = True
    replace = False
    if old_amt is None and new_amt is not None:
        replace = True
    elif old_amt is not None:
        try:
            of = float(old_amt)
        except (TypeError, ValueError):
            of = None
        if of is not None:
            if amount_is_ref_id(
                old.get("text_sample") or "", of, str(old.get("utr") or "")
            ) and (new_amt is None or abs(float(new_amt) - of) > 0.001):
                replace = True
            elif of < 100 and new_amt is not None and float(new_amt) >= 100:
                replace = True
            else:
                nxt, reason = proven_amount_correction(
                    text, of, utr, payload.get("amount_tesseract_raw")
                )
                if reason and (
                    (nxt is None and new_amt is None)
                    or (new_amt is not None and nxt is not None and abs(float(new_amt) - float(nxt)) < 0.011)
                    or (new_amt is not None and nxt is not None)
                ):
                    replace = True
    if not replace:
        # Still adopt missing UTR/VPA from a fuller OCR pass.
        changed = False
        merged = dict(old)
        if not merged.get("utr") and payload.get("utr"):
            merged["utr"] = payload["utr"]
            changed = True
        if not merged.get("vpa") and payload.get("vpa"):
            merged["vpa"] = payload["vpa"]
            merged["vpas"] = payload.get("vpas") or [payload["vpa"]]
            changed = True
        if payload.get("text_sample") and len(payload["text_sample"]) > len(
            merged.get("text_sample") or ""
        ):
            merged["text_sample"] = payload["text_sample"]
            changed = True
        return merged if changed else None
    merged = dict(old)
    merged.update({k: v for k, v in payload.items() if v is not None or k in ("amount_inr", "amount_value")})
    if old_amt is not None:
        merged.setdefault("amount_tesseract_raw", old_amt)
    merged["kind"] = old.get("kind") or payload.get("kind")
    merged["engine"] = payload.get("engine") or merged.get("engine")
    merged["reocr"] = True
    if new_amt is None:
        merged["amount_inr"] = None
        merged["amount_value"] = None
    else:
        merged["amount_inr"] = new_amt
        merged["amount_value"] = new_amt
    if not merged.get("utr") and old.get("utr"):
        merged["utr"] = old["utr"]
    if not merged.get("vpa") and old.get("vpa"):
        merged["vpa"] = old["vpa"]
    return merged


def _one(store: GCSMirrorClient, key: str, old: dict) -> tuple[str, dict | None, str]:
    kind = classify(key)
    if kind not in ("deposit", "withdrawal"):
        return key, None, "skip"
    try:
        data = store.get_object_bytes(key)
    except Exception as e:
        return key, None, f"fetch:{e}"[:120]
    try:
        payload = ocr_slip(data, allow_gemini=True)
    except Exception as e:
        return key, None, f"ocr:{e}"[:120]
    payload["kind"] = kind
    merged = _better(old, payload)
    return key, merged, "ok" if merged else "unchanged"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = all suspects")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args()
    store = GCSMirrorClient(config.GCS_BUCKET, config.GCS_PREFIX)
    c = db.connect()
    rows = c.execute(
        "SELECT key, extract_json FROM objects"
        " WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind') IN ('deposit','withdrawal')"
    ).fetchall()
    jobs: list[tuple[str, dict]] = []
    seen = set()
    for k in EXHIBITS:
        seen.add(k)
    for r in rows:
        try:
            p = json.loads(r["extract_json"])
        except Exception:
            continue
        key = r["key"]
        if key in seen or _is_suspect(p):
            jobs.append((key, p))
            seen.add(key)
        if args.limit and len(jobs) >= args.limit:
            break
    # Exhibits first even if they were not in the SELECT (still in DB).
    for k in reversed(EXHIBITS):
        row = c.execute(
            "SELECT extract_json FROM objects WHERE key=?", (k,)
        ).fetchone()
        if not row:
            continue
        try:
            p = json.loads(row["extract_json"])
        except Exception:
            continue
        jobs = [(k, p)] + [(xk, xp) for xk, xp in jobs if xk != k]
    print(f"suspects={len(jobs)} workers={args.workers}", flush=True)
    pending: list[tuple[str, dict]] = []
    done = wrote = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = [pool.submit(_one, store, k, p) for k, p in jobs]
        for fut in as_completed(futs):
            key, merged, status = fut.result()
            done += 1
            if merged is not None:
                pending.append((key, merged))
                wrote += 1
            elif status not in ("ok", "unchanged", "skip"):
                failed += 1
            if len(pending) >= BATCH:
                db.persist_enriched_batch(pending)
                pending = []
                print(
                    f"flushed wrote={wrote} done={done} failed={failed}",
                    flush=True,
                )
    if pending:
        db.persist_enriched_batch(pending)
    print(f"wrote={wrote} done={done} failed={failed} suspects={len(jobs)}")
    res = db.rebuild_payment_index(force=True)
    print(json.dumps(res, ensure_ascii=False)[:500])


if __name__ == "__main__":
    main()
