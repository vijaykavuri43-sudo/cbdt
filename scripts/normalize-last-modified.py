#!/usr/bin/env python3
"""One-time repair: normalize non-ISO last_modified values in manifest.db.

CASE-2026-CBTF · STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING · READ-ONLY bucket access

Why: the GCS mirror-sync stored the original S3 LastModified in custom object
metadata as RFC-1123 HTTP-dates ('Wed, 31 Jul 2024 21:45:23 GMT'). Rows swept
from the mirror kept those strings verbatim, and because the manifest sorts
lexicographically, every order=-modified view (Chat, Deposits, Withdrawals,
Overview newest-first) showed 2024 RFC-dated objects on top while genuinely
new ISO-dated 2026 objects sank to the bottom — making live operator activity
look like stale 2024/2025 data.

Fix: rewrite any last_modified that isn't already ISO ('YYYY-MM-DDTHH:MM:SSZ')
to normalized ISO UTC using the same parser the collector now uses
(backend.db.normalize_object_ts). Rows that cannot be parsed are left untouched
and reported.

Run locally:
    python3 s3-live-monitor/scripts/normalize-last-modified.py
Run on the deployed VM (container name s3lm, db on the s3lm-data volume):
    gcloud --account=jk5391775@gmail.com compute ssh s3lm-budget \
      --project=project-9641a7b9-3759-4c98-9b8 --zone=asia-south1-a -- \
      'docker cp s3lm:/dev/null /dev/null; docker exec s3lm python3 /app/scripts/normalize-last-modified.py'
(scripts/ is not in the image — docker cp this file in first, or pipe it:
    cat normalize-last-modified.py | docker exec -i s3lm python3 - )
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

# Allow running both as a plain script and as a module inside the container.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.db import normalize_object_ts  # noqa: E402

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def main() -> int:
    from backend import config

    db_path = config.DB_PATH
    print(f"[*] manifest: {db_path}")
    c = sqlite3.connect(str(db_path))
    c.execute("PRAGMA journal_mode=WAL")

    rows = c.execute(
        "SELECT key, last_modified FROM objects "
        "WHERE last_modified != '' AND last_modified NOT LIKE '____-__-__T__:__:__Z'"
    ).fetchall()
    print(f"[*] rows with non-ISO last_modified: {len(rows)}")
    if not rows:
        print("[+] nothing to repair")
        return 0

    updates: list[tuple[str, str]] = []
    unparseable: list[tuple[str, str]] = []
    for key, lm in rows:
        fixed = normalize_object_ts(lm)
        if fixed == lm or not ISO_RE.match(fixed):
            unparseable.append((key, lm))
        else:
            updates.append((fixed, key))

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with c:
        c.executemany("UPDATE objects SET last_modified = ? WHERE key = ?", updates)
        c.execute(
            "INSERT INTO meta(k,v) VALUES('ts_normalize_utc',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (now,),
        )
    c.commit()
    c.close()

    print(f"[+] normalized rows : {len(updates)}")
    print(f"[!] unparseable rows: {len(unparseable)}")
    for k, lm in unparseable[:10]:
        print(f"    {lm!r}  {k[:80]}")
    print("[*] restart the backend so in-memory state picks up corrected values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
