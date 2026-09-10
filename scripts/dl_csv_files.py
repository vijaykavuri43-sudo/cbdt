#!/usr/bin/env python3
"""Bulk-download csv-files/ ledger prefix from public S3 bucket (read-only GET).

Auth: STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING. Writes chain-of-custody manifest.
Usage: python3 dl_csv_files.py  (runs ~21k files / ~4.4GB; resumable, skips done)
"""

import hashlib, json, os, sys, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

_tls = threading.local()


def sess():
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "LE-evidence-collector"
        _tls.s = s
    return s


BUCKET = "https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com"
PREFIX = "csv-files/"
OUTDIR = os.path.join(
    "intelligence",
    "analysis",
    "CASE-2026-CBTF",
    "live-monitor",
    "2026-09-09",
    "csv-files",
)
MANIFESTL = os.path.join(OUTDIR, "_MANIFEST.jsonl")
SUMMARY = os.path.join(OUTDIR, "_MANIFEST.json")
PROGRESS = os.path.join(OUTDIR, "_progress.json")
WORKERS = 32
NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}

os.makedirs(OUTDIR, exist_ok=True)


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_all():
    keys, token = [], None
    while True:
        q = {"list-type": "2", "prefix": PREFIX, "max-keys": "1000"}
        if token:
            q["continuation-token"] = token
        url = BUCKET + "/?" + urllib.parse.urlencode(q)
        raw = ""
        for attempt in range(5):
            try:
                raw = urllib.request.urlopen(url, timeout=30).read().decode()
                break
            except Exception as e:
                if attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))
        r = ET.fromstring(raw)
        for c in r.findall("s:Contents", NS):
            k = c.findtext("s:Key", namespaces=NS)
            if k and not k.endswith("/"):
                keys.append((k, int(c.findtext("s:Size", namespaces=NS) or 0)))
        trunc = r.findtext("s:IsTruncated", namespaces=NS) == "true"
        token = r.findtext("s:NextContinuationToken", namespaces=NS) if trunc else None
        if not trunc:
            break
    return keys


def done_set():
    done = {}
    n_err = 0
    if os.path.exists(MANIFESTL):
        with open(MANIFESTL) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if "sha256" in d:  # only true successes count; errors retried
                        done[d["key"]] = d
                    else:
                        n_err += 1
                except Exception:
                    pass
    return done


def fetch(key, size):
    rel = key[len(PREFIX) :]
    dest = os.path.join(OUTDIR, rel)
    if os.path.exists(dest) and os.path.getsize(dest) == size and size > 0:
        return key, "skip", size
    os.makedirs(os.path.dirname(dest) or OUTDIR, exist_ok=True)
    url = BUCKET + "/" + urllib.parse.quote(key, safe="/-._~")
    last = None
    for attempt in range(4):
        try:
            r = sess().get(url, timeout=(15, 120), stream=True)
            r.raise_for_status()
            h = hashlib.sha256()
            n = 0
            with open(dest + ".part", "wb") as f:
                for chunk in r.iter_content(262144):
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
            r.close()
            if size and n != size:
                last = f"size-mismatch {n}!={size}"
                continue
            os.replace(dest + ".part", dest)
            return key, "ok:" + h.hexdigest(), n
        except Exception as e:
            last = str(e)[:120]
            _tls.s = None  # drop poisoned keep-alive connection
            time.sleep(2 * (attempt + 1))
    return key, "error:" + str(last), 0


def main():
    print("listing...", flush=True)
    keys = list_all()
    total_bytes = sum(s for _, s in keys)
    print(f"listed {len(keys)} keys, {total_bytes / 1e9:.2f} GB", flush=True)
    with open(os.path.join(OUTDIR, "_keylist.json"), "w") as f:
        json.dump(
            {
                "listed_utc": utcnow(),
                "count": len(keys),
                "bytes": total_bytes,
                "keys": [k for k, _ in keys],
            },
            f,
        )
    done = done_set()
    todo = [(k, s) for k, s in keys if k not in done]
    print(f"resuming: {len(done)} done, {len(todo)} to fetch", flush=True)
    man = open(MANIFESTL, "a")
    ok = err = skip = 0
    t0 = time.time()

    def one(item):
        return fetch(*item)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, (key, st, n) in enumerate(ex.map(one, todo)):
            if st.startswith("ok:"):
                ok += 1
                man.write(
                    json.dumps(
                        {
                            "key": key,
                            "size": n,
                            "sha256": st[3:],
                            "utc": utcnow(),
                            "method": "public-S3-GET",
                            "auth": "STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING",
                        }
                    )
                    + "\n"
                )
            elif st == "skip":
                skip += 1
            else:
                err += 1
                man.write(json.dumps({"key": key, "error": st, "utc": utcnow()}) + "\n")
            if (i + 1) % 250 == 0:
                man.flush()
                el = time.time() - t0
                print(
                    f"{i + 1}/{len(todo)} ok={ok} skip(extra)={skip} err={err} {el:.0f}s",
                    flush=True,
                )
                with open(PROGRESS, "w") as pf:
                    json.dump(
                        {
                            "done": i + 1,
                            "todo": len(todo),
                            "ok": ok,
                            "errors": err,
                            "utc": utcnow(),
                        },
                        pf,
                    )
    man.close()
    summary = {
        "completed_utc": utcnow(),
        "listed": len(keys),
        "listed_bytes": total_bytes,
        "newly_ok": ok,
        "previously_done": len(done),
        "errors": err,
        "auth": "STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING",
    }
    with open(SUMMARY, "w") as f:
        json.dump(summary, f, indent=1)
    print("SUMMARY: " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
