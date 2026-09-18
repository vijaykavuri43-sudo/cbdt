#!/usr/bin/env python3
"""Re-extract amount/VPA/UTR from stored OCR text_sample — no re-OCR.

Fixes rupee-eaten amounts (₹3,000 stored as 23,000), null amounts recoverable
from the sample, and missed UTRs. Batches writes so 400k slips finish in
minutes rather than hours.

Run inside the s3lm container after deploy:
  python3 /app/scripts/reparse-from-text.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import db, enrich  # noqa: E402

BATCH = 200


def main() -> None:
    c = db.connect()
    rows = c.execute(
        "SELECT key, extract_json FROM objects"
        " WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind') IN ('deposit','withdrawal')"
    ).fetchall()
    pending: list[tuple[str, dict]] = []
    scanned = updated = 0
    for r in rows:
        try:
            p = json.loads(r["extract_json"])
        except Exception:
            continue
        text = (p.get("text_sample") or "").strip()
        if not text:
            continue
        scanned += 1
        changed = False
        amt = enrich.extract_amount(text)
        cur = p.get("amount_inr")
        raw = p.get("amount_tesseract_raw")
        eaten = None
        if cur is not None:
            eaten = enrich.correct_rupee_eaten(text, float(cur))
        if eaten is None and raw is not None:
            try:
                eaten = enrich.correct_rupee_eaten(text, float(raw))
            except (TypeError, ValueError):
                eaten = None
        if eaten is not None:
            if cur is None or abs(float(cur) - eaten) > 0.001:
                p["amount_tesseract_raw"] = cur if cur is not None else raw
                p["rupee_eaten"] = True
                p["amount_inr"] = eaten
                p["amount_value"] = eaten
                changed = True
        elif cur is None and amt is not None:
            p["amount_inr"] = amt
            p["amount_value"] = amt
            changed = True
        elif (
            cur is not None
            and amt is not None
            and float(cur) < 100
            and amt >= 100
            and abs(float(cur) - amt) > 0.001
        ):
            # Restore rows a prior reparse smashed with OCR debris (~4).
            p["amount_tesseract_raw"] = raw or cur
            p["amount_inr"] = amt
            p["amount_value"] = amt
            changed = True
        elif cur is not None and enrich.amount_is_ref_id(
            text, float(cur), str(p.get("utr") or "")
        ):
            # Paytm/UPI ref truncated into rupees (₹150 stored as 31,49,658).
            p["amount_tesseract_raw"] = cur if cur is not None else raw
            p["ref_as_amount"] = True
            if amt is not None and abs(float(cur) - amt) > 0.001:
                p["amount_inr"] = amt
                p["amount_value"] = amt
            else:
                p["amount_inr"] = None
                p["amount_value"] = None
            changed = True
        vpas = enrich.extract_vpas(text)
        if vpas:
            v = vpas[0]
            if p.get("vpa") != v:
                p["vpa"] = v
                p["vpas"] = vpas
                p["vpa_details"] = enrich.extract_vpa_details(text)
                changed = True
        if not p.get("utr"):
            mu = enrich.UTR_RE.search(text)
            if mu:
                cand = mu.group(1).strip().upper()
                digits = re.sub(r"\D", "", cand)
                if len(digits) == 12:
                    p["utr"] = digits
                    changed = True
                elif 8 <= len(cand) <= 16 and re.match(r"^[A-Z0-9]{8,16}$", cand):
                    p["utr"] = cand
                    changed = True
            elif not p.get("utr"):
                for mn in enrich.NUM12_RE.finditer(text):
                    before = text[max(0, mn.start() - 40) : mn.start()].lower()
                    if enrich._ACCOUNT_KEYWORDS_RE.search(before):
                        continue
                    p["utr"] = mn.group(1)
                    changed = True
                    break
        if changed:
            pending.append((r["key"], p))
            updated += 1
            if len(pending) >= BATCH:
                db.persist_enriched_batch(pending)
                pending = []
                print(f"flushed updated={updated} scanned={scanned}", flush=True)
    if pending:
        db.persist_enriched_batch(pending)
    print(f"updated={updated} scanned={scanned} rows")
    res = db.rebuild_payment_index(force=True)
    print(json.dumps(res, ensure_ascii=False)[:500])


if __name__ == "__main__":
    main()
