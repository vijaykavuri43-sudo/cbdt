#!/usr/bin/env python3
"""Re-extract amount/VPA/UTR from stored OCR text_sample — no re-OCR.

Fixes rows where tesseract misread the rupee glyph as '=' (amount_inr null),
spaced/thousand-separator amounts, or missed UTRs, using the already-persisted
OCR text. Then force-rebuilds the normalized payment index.

Run inside the s3lm container:
  python3 /app/scripts/reparse-from-text.py
(Deploy first so the updated AMOUNT_PATTERNS / _sanitize_amount are live.)
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, db, enrich  # noqa: E402


def main() -> None:
    c = db.connect()
    rows = c.execute(
        "SELECT key, extract_json FROM objects"
        " WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind') IN ('deposit','withdrawal')"
    ).fetchall()
    updated = 0
    for r in rows:
        try:
            p = json.loads(r["extract_json"])
        except Exception:
            continue
        text = (p.get("text_sample") or "").strip()
        if not text:
            continue
        changed = False
        amt = enrich.extract_amount(text)
        cur = p.get("amount_inr")
        if amt is not None and (cur is None or abs(float(cur) - amt) > 0.001):
            p["amount_inr"] = amt
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
                    before = text[max(0, mn.start() - 40): mn.start()].lower()
                    if enrich._ACCOUNT_KEYWORDS_RE.search(before):
                        continue
                    p["utr"] = mn.group(1)
                    changed = True
                    break
        if changed:
            db.mark_enriched(r["key"], p)
            updated += 1
    print(f"updated={updated} rows")
    res = db.rebuild_payment_index(force=True)
    print(json.dumps(res, ensure_ascii=False)[:500])


if __name__ == "__main__":
    main()