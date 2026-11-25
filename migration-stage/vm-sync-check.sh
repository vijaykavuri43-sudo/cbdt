#!/bin/bash
# Read-only post-migration verification for the s3-live-monitor VM.
# Historical October three-source report; current setup has four sources.
# Current operator is jk5391775@gmail.com; see INFRA.md for current organization.
set -u

DOCKER="docker"
if ! docker ps >/dev/null 2>&1; then
  DOCKER="sudo -n docker"
fi

echo "===== HOST ====="
hostname
date -u +%Y-%m-%dT%H:%M:%SZ
uptime
echo "===== DISK ====="
df -h / /var/lib/docker 2>/dev/null || df -h /
echo "===== CONTAINERS ====="
$DOCKER ps --format '{{.Names}}|{{.Status}}|{{.Image}}' 2>&1 || true
echo "===== ALL CONTAINERS ====="
$DOCKER ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}' 2>&1 || true
echo "===== INSPECT ====="
for c in s3lm s3sync s3lm-b2 s3sync-b2 s3lm-b3 s3sync-b3; do
  $DOCKER inspect --format '{{.Name}}|state={{.State.Status}}|started={{.State.StartedAt}}|health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|restarts={{.RestartCount}}' "$c" 2>&1 || true
done

echo "===== HEALTH ENDPOINTS ====="
for port in 80 81 82; do
  code=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/healthz" 2>/dev/null || echo ERR)
  echo "port $port healthz=$code"
done

echo "===== DASHBOARD SYNC STATUS ====="
for spec in "80 s3lm" "81 s3lm-b2" "82 s3lm-b3"; do
  set -- $spec
  port=$1
  c=$2
  tok=$($DOCKER exec "$c" cat /root/.local/share/s3-live-monitor/access-token.txt 2>/dev/null | tr -d '\r\n')
  echo "--- $c port=$port token=$([ -n "$tok" ] && echo present || echo missing)"
  if [ -n "$tok" ]; then
    curl -sS -m 40 -H "Authorization: Bearer $tok" "http://127.0.0.1:$port/api/mirror-sync" \
      | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("parse_error:", e)
    raise SystemExit
keys = ["daemon_seen","daemon_state","healthy","status_age_min","tombstones_pending","verdict","error"]
print(json.dumps({k:d.get(k) for k in keys}, indent=2, sort_keys=True))
s = d.get("status") or {}
skeys = ["utc","cycle","state","writes_blocked","writes_block_reason","queued_pending",
         "tombstones_pending","tombstones_terminal","cycle_took_sec","scanned_this_cycle",
         "scanned_total","queued","uploaded_total","bytes_total","errors",
         "dashboard_notified","dashboard_notify_errors","oversized_skipped",
         "gcs_mirror_objects","preserved_only_estimate","source_inventory_objects",
         "source_inventory_bytes","source_inventory_prefixes_scanned",
         "source_inventory_prefixes_total","source_inventory_complete",
         "source_inventory_oldest_scan_utc","source_inventory_newest_scan_utc",
         "s3_bucket","gcs_target"]
print(json.dumps({k:s.get(k) for k in skeys if k in s}, indent=2, sort_keys=True))
' 2>&1 || true
  fi
done

echo "===== RECENT MANIFEST ARRIVALS (2h) ====="
for spec in "80 s3lm" "81 s3lm-b2" "82 s3lm-b3"; do
  set -- $spec
  port=$1
  c=$2
  tok=$($DOCKER exec "$c" cat /root/.local/share/s3-live-monitor/access-token.txt 2>/dev/null | tr -d '\r\n')
  echo "--- $c"
  if [ -n "$tok" ]; then
    curl -sS -m 30 -H "Authorization: Bearer $tok" "http://127.0.0.1:$port/api/recent?hours=2&limit=8" \
      | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for x in d.get("items", []):
        print(json.dumps({k:x.get(k) for k in ("key","size","last_modified","seen_utc","enriched")}, sort_keys=True))
except Exception as e:
    print("parse_error:", e)
' 2>&1 || true
  fi
done

echo "===== APP DB: s3lm ====="
$DOCKER exec -i s3lm python3 - <<'PY' 2>&1 || true
import json, sqlite3
DB = "/root/.local/share/s3-live-monitor/manifest.db"
con = sqlite3.connect(DB, timeout=30)
con.execute("PRAGMA query_only=ON")
def one(sql, args=()):
    try:
        return con.execute(sql, args).fetchone()[0]
    except Exception as e:
        return f"ERR: {e}"
def rows(sql, args=(), n=10):
    try:
        return con.execute(sql, args).fetchall()[:n]
    except Exception as e:
        return [("ERR", str(e))]
out = {}
out["objects_total"] = one("SELECT COUNT(*) FROM objects")
out["enriched"] = one("SELECT COUNT(*) FROM objects WHERE enriched=1")
out["pending_enrich"] = one("SELECT COUNT(*) FROM objects WHERE enriched=0 AND extract_json IS NULL")
out["enrich_errors"] = one("SELECT COUNT(*) FROM objects WHERE enrich_error IS NOT NULL AND enrich_error != ''")
out["seen_2h"] = one("SELECT COUNT(*) FROM objects WHERE seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')")
out["seen_24h"] = one("SELECT COUNT(*) FROM objects WHERE seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours')")
out["enriched_2h"] = one("SELECT COUNT(*) FROM objects WHERE enriched=1 AND enriched_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')")
out["max_seen_utc"] = one("SELECT MAX(seen_utc) FROM objects")
out["max_last_modified"] = one("SELECT MAX(last_modified) FROM objects")
out["max_enriched_utc"] = one("SELECT MAX(enriched_utc) FROM objects")
out["recent_objects"] = rows("SELECT key,size,seen_utc,enriched,enriched_utc FROM objects ORDER BY seen_utc DESC LIMIT 8")
out["top_prefixes"] = rows("SELECT top_prefix,COUNT(*) FROM objects GROUP BY top_prefix ORDER BY 2 DESC LIMIT 10")
out["table_counts"] = {}
for t in ("payment_transactions","wallet_hits","ledger_files","ledger_rows","gateway_files","gateway_rows","utr_identity"):
    out["table_counts"][t] = one(f"SELECT COUNT(*) FROM {t}")
out["meta"] = rows("SELECT k,v FROM meta ORDER BY k LIMIT 30")
print(json.dumps(out, indent=2, default=str, sort_keys=True))
PY

echo "===== APP DB: s3lm-b2 ====="
$DOCKER exec -i s3lm-b2 python3 - <<'PY' 2>&1 || true
import json, sqlite3
DB = "/root/.local/share/s3-live-monitor/manifest.db"
con = sqlite3.connect(DB, timeout=30)
con.execute("PRAGMA query_only=ON")
def one(sql):
    try:
        return con.execute(sql).fetchone()[0]
    except Exception as e:
        return f"ERR: {e}"
def rows(sql, n=10):
    try:
        return con.execute(sql).fetchall()[:n]
    except Exception as e:
        return [("ERR", str(e))]
out = {}
out["objects_total"] = one("SELECT COUNT(*) FROM objects")
out["enriched"] = one("SELECT COUNT(*) FROM objects WHERE enriched=1")
out["pending_enrich"] = one("SELECT COUNT(*) FROM objects WHERE enriched=0 AND extract_json IS NULL")
out["enrich_errors"] = one("SELECT COUNT(*) FROM objects WHERE enrich_error IS NOT NULL AND enrich_error != ''")
out["seen_2h"] = one("SELECT COUNT(*) FROM objects WHERE seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')")
out["seen_24h"] = one("SELECT COUNT(*) FROM objects WHERE seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours')")
out["enriched_2h"] = one("SELECT COUNT(*) FROM objects WHERE enriched=1 AND enriched_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')")
out["max_seen_utc"] = one("SELECT MAX(seen_utc) FROM objects")
out["max_last_modified"] = one("SELECT MAX(last_modified) FROM objects")
out["max_enriched_utc"] = one("SELECT MAX(enriched_utc) FROM objects")
out["recent_objects"] = rows("SELECT key,size,seen_utc,enriched,enriched_utc FROM objects ORDER BY seen_utc DESC LIMIT 8")
out["top_prefixes"] = rows("SELECT top_prefix,COUNT(*) FROM objects GROUP BY top_prefix ORDER BY 2 DESC LIMIT 10")
out["table_counts"] = {}
for t in ("payment_transactions","wallet_hits","ledger_files","ledger_rows","gateway_files","gateway_rows","utr_identity"):
    out["table_counts"][t] = one(f"SELECT COUNT(*) FROM {t}")
out["meta"] = rows("SELECT k,v FROM meta ORDER BY k LIMIT 30")
print(json.dumps(out, indent=2, default=str, sort_keys=True))
PY

echo "===== APP DB: s3lm-b3 ====="
$DOCKER exec -i s3lm-b3 python3 - <<'PY' 2>&1 || true
import json, sqlite3
DB = "/root/.local/share/s3-live-monitor/manifest.db"
con = sqlite3.connect(DB, timeout=30)
con.execute("PRAGMA query_only=ON")
def one(sql):
    try:
        return con.execute(sql).fetchone()[0]
    except Exception as e:
        return f"ERR: {e}"
def rows(sql, n=10):
    try:
        return con.execute(sql).fetchall()[:n]
    except Exception as e:
        return [("ERR", str(e))]
out = {}
out["objects_total"] = one("SELECT COUNT(*) FROM objects")
out["enriched"] = one("SELECT COUNT(*) FROM objects WHERE enriched=1")
out["pending_enrich"] = one("SELECT COUNT(*) FROM objects WHERE enriched=0 AND extract_json IS NULL")
out["enrich_errors"] = one("SELECT COUNT(*) FROM objects WHERE enrich_error IS NOT NULL AND enrich_error != ''")
out["seen_2h"] = one("SELECT COUNT(*) FROM objects WHERE seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')")
out["seen_24h"] = one("SELECT COUNT(*) FROM objects WHERE seen_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours')")
out["enriched_2h"] = one("SELECT COUNT(*) FROM objects WHERE enriched=1 AND enriched_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours')")
out["max_seen_utc"] = one("SELECT MAX(seen_utc) FROM objects")
out["max_last_modified"] = one("SELECT MAX(last_modified) FROM objects")
out["max_enriched_utc"] = one("SELECT MAX(enriched_utc) FROM objects")
out["recent_objects"] = rows("SELECT key,size,seen_utc,enriched,enriched_utc FROM objects ORDER BY seen_utc DESC LIMIT 8")
out["top_prefixes"] = rows("SELECT top_prefix,COUNT(*) FROM objects GROUP BY top_prefix ORDER BY 2 DESC LIMIT 10")
out["table_counts"] = {}
for t in ("payment_transactions","wallet_hits","ledger_files","ledger_rows","gateway_files","gateway_rows","utr_identity"):
    out["table_counts"][t] = one(f"SELECT COUNT(*) FROM {t}")
out["meta"] = rows("SELECT k,v FROM meta ORDER BY k LIMIT 30")
print(json.dumps(out, indent=2, default=str, sort_keys=True))
PY

echo "===== SYNC STATE: s3sync ====="
$DOCKER exec -i s3sync python3 - <<'PY' 2>&1 || true
import json, sqlite3
DB = "/root/.local/share/s3-sync/state.db"
con = sqlite3.connect(DB, timeout=30)
con.execute("PRAGMA query_only=ON")
def one(sql):
    try:
        return con.execute(sql).fetchone()[0]
    except Exception as e:
        return f"ERR: {e}"
def rows(sql, n=10):
    try:
        return con.execute(sql).fetchall()[:n]
    except Exception as e:
        return [("ERR", str(e))]
out = {}
out["synced"] = one("SELECT COUNT(*) FROM synced")
out["failures_open"] = one("SELECT COUNT(*) FROM sync_failures WHERE terminal=0")
out["failures_terminal"] = one("SELECT COUNT(*) FROM sync_failures WHERE terminal=1")
out["failures_retrying"] = one("SELECT COUNT(*) FROM sync_failures WHERE fail_count>0 AND terminal=0")
out["max_synced_utc"] = one("SELECT MAX(synced_utc) FROM synced")
out["recent_synced"] = rows("SELECT key,synced_utc,gcs_generation FROM synced ORDER BY synced_utc DESC LIMIT 8")
out["recent_failures"] = rows("SELECT key,fail_count,terminal,last_attempt_utc,substr(last_error,1,140) FROM sync_failures ORDER BY last_attempt_utc DESC LIMIT 8")
out["prefix_inventory"] = rows("SELECT prefix,object_count,bytes,newest_utc,completed_utc FROM prefix_inventory ORDER BY completed_utc DESC LIMIT 12")
print(json.dumps(out, indent=2, default=str, sort_keys=True))
PY

echo "===== SYNC STATE: s3sync-b2 ====="
$DOCKER exec -i s3sync-b2 python3 - <<'PY' 2>&1 || true
import json, sqlite3
DB = "/root/.local/share/s3-sync/state.db"
con = sqlite3.connect(DB, timeout=30)
con.execute("PRAGMA query_only=ON")
def one(sql):
    try:
        return con.execute(sql).fetchone()[0]
    except Exception as e:
        return f"ERR: {e}"
def rows(sql, n=10):
    try:
        return con.execute(sql).fetchall()[:n]
    except Exception as e:
        return [("ERR", str(e))]
out = {}
out["synced"] = one("SELECT COUNT(*) FROM synced")
out["failures_open"] = one("SELECT COUNT(*) FROM sync_failures WHERE terminal=0")
out["failures_terminal"] = one("SELECT COUNT(*) FROM sync_failures WHERE terminal=1")
out["failures_retrying"] = one("SELECT COUNT(*) FROM sync_failures WHERE fail_count>0 AND terminal=0")
out["max_synced_utc"] = one("SELECT MAX(synced_utc) FROM synced")
out["recent_synced"] = rows("SELECT key,synced_utc,gcs_generation FROM synced ORDER BY synced_utc DESC LIMIT 8")
out["recent_failures"] = rows("SELECT key,fail_count,terminal,last_attempt_utc,substr(last_error,1,140) FROM sync_failures ORDER BY last_attempt_utc DESC LIMIT 8")
out["prefix_inventory"] = rows("SELECT prefix,object_count,bytes,newest_utc,completed_utc FROM prefix_inventory ORDER BY completed_utc DESC LIMIT 12")
print(json.dumps(out, indent=2, default=str, sort_keys=True))
PY

echo "===== SYNC STATE: s3sync-b3 ====="
$DOCKER exec -i s3sync-b3 python3 - <<'PY' 2>&1 || true
import json, sqlite3
DB = "/root/.local/share/s3-sync/state.db"
con = sqlite3.connect(DB, timeout=30)
con.execute("PRAGMA query_only=ON")
def one(sql):
    try:
        return con.execute(sql).fetchone()[0]
    except Exception as e:
        return f"ERR: {e}"
def rows(sql, n=10):
    try:
        return con.execute(sql).fetchall()[:n]
    except Exception as e:
        return [("ERR", str(e))]
out = {}
out["synced"] = one("SELECT COUNT(*) FROM synced")
out["failures_open"] = one("SELECT COUNT(*) FROM sync_failures WHERE terminal=0")
out["failures_terminal"] = one("SELECT COUNT(*) FROM sync_failures WHERE terminal=1")
out["failures_retrying"] = one("SELECT COUNT(*) FROM sync_failures WHERE fail_count>0 AND terminal=0")
out["max_synced_utc"] = one("SELECT MAX(synced_utc) FROM synced")
out["recent_synced"] = rows("SELECT key,synced_utc,gcs_generation FROM synced ORDER BY synced_utc DESC LIMIT 8")
out["recent_failures"] = rows("SELECT key,fail_count,terminal,last_attempt_utc,substr(last_error,1,140) FROM sync_failures ORDER BY last_attempt_utc DESC LIMIT 8")
out["prefix_inventory"] = rows("SELECT prefix,object_count,bytes,newest_utc,completed_utc FROM prefix_inventory ORDER BY completed_utc DESC LIMIT 12")
print(json.dumps(out, indent=2, default=str, sort_keys=True))
PY

echo "===== SYNC LOG: s3sync (last 80) ====="
$DOCKER logs --tail 80 s3sync 2>&1 || true
echo "===== SYNC LOG: s3sync-b2 (last 80) ====="
$DOCKER logs --tail 80 s3sync-b2 2>&1 || true
echo "===== SYNC LOG: s3sync-b3 (last 80) ====="
$DOCKER logs --tail 80 s3sync-b3 2>&1 || true

echo "===== APP LOG ERRORS: s3lm (last 60 matching) ====="
$DOCKER logs --tail 300 s3lm 2>&1 | grep -Ei 'error|traceback|exception|failed|enrich|ocr|persist|sync' | tail -60 || true
echo "===== APP LOG ERRORS: s3lm-b2 (last 60 matching) ====="
$DOCKER logs --tail 300 s3lm-b2 2>&1 | grep -Ei 'error|traceback|exception|failed|enrich|ocr|persist|sync' | tail -60 || true
echo "===== APP LOG ERRORS: s3lm-b3 (last 60 matching) ====="
$DOCKER logs --tail 300 s3lm-b3 2>&1 | grep -Ei 'error|traceback|exception|failed|enrich|ocr|persist|sync' | tail -60 || true

echo "===== DONE ====="
