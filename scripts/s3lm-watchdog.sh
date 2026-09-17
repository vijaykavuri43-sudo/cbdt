#!/usr/bin/env bash
# Tolerant s3lm watchdog — replaces the hair-trigger cron that force-restarted
# the dashboard ~10x/day (review 2026-09-17).
#
# Old line (in /etc/cron.d/s3lm-watchdog):
#   * * * * * root curl -s --max-time 10 -o /dev/null http://localhost:80/api/healthz || docker restart s3lm
# Why it hurt: ONE failed check (a >10s transient stall, or the restart window
# itself returning connection-refused) ran `docker restart`; the app cannot
# exit in 10s (OCR threads + SQLite WAL), so every restart ended in SIGKILL
# (exit 137) and each restart window tripped the next check — a restart loop
# that also starved s3sync notifies (1000+ "dashboard notify deferred").
#
# New behavior:
#   - 3 attempts per run (25s timeout, -f so HTTP 5xx counts as failure);
#   - restart only after 3 CONSECUTIVE failed runs (state file);
#   - skip while the container started <5 min ago (startup grace);
#   - restart with `-t 55` so SQLite/WAL gets a graceful shutdown;
#   - log every restart decision to syslog.
# Cron line (installed by scripts/install-watchdog.sh, flock avoids overlap —
# worst-case run is ~85s, longer than the 1-min cadence):
#   * * * * * root flock -n /var/tmp/s3lm-watchdog.lock /usr/local/bin/s3lm-watchdog.sh
set -u

CONTAINER="${WATCHDOG_CONTAINER:-s3lm}"
URL="${WATCHDOG_URL:-http://localhost:80/api/healthz}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/var/tmp/s3lm-watchdog}"
FAIL_FILE="$STATE_DIR/fails"
COOLDOWN_SEC=300
NEED_CONSECUTIVE=3
TIMEOUT_SEC=25
RESTART_GRACE_SEC=55

mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

# Container missing -> nothing to watch (deploy/bootstrap owns creation).
started_at="$(docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null)" || exit 0
status="${started_at%%|*}"
[ "$status" = "running" ] || exit 0
started="${started_at#*|}"
start_epoch="$(date -d "$started" +%s 2>/dev/null || echo 0)"
now_epoch="$(date +%s)"
if [ "$start_epoch" -gt 0 ] && [ $((now_epoch - start_epoch)) -lt "$COOLDOWN_SEC" ]; then
  exit 0  # startup grace: healthz is meaningless mid-boot
fi

fails=0
if [ -f "$FAIL_FILE" ]; then
  fails="$(cat "$FAIL_FILE" 2>/dev/null || echo 0)"
  case "$fails" in ''|*[!0-9]*) fails=0 ;; esac
fi

ok=0
for _ in 1 2 3; do
  if curl -fsS --max-time "$TIMEOUT_SEC" -o /dev/null "$URL" 2>/dev/null; then
    ok=1
    break
  fi
  sleep 5
done

if [ "$ok" = 1 ]; then
  [ "$fails" != "0" ] && echo 0 > "$FAIL_FILE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$FAIL_FILE"
if [ "$fails" -ge "$NEED_CONSECUTIVE" ]; then
  logger -t s3lm-watchdog "healthz failed ${fails}x consecutive — restarting ${CONTAINER}"
  echo 0 > "$FAIL_FILE"
  docker restart -t "$RESTART_GRACE_SEC" "$CONTAINER" >/dev/null 2>&1 || true
fi
