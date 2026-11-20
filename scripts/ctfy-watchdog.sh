#!/usr/bin/env bash
# Host watchdog: restarts the campaign when the process is gone or hung.
#
# Install as a systemd timer (every 5 minutes). The campaign lock decides
# whether a run is live; the heartbeat file proves the commander loop is
# still making progress. A live lock with a stale heartbeat is treated as a
# hang: the play process holding the lock is killed and the launcher retries.
set -euo pipefail

PROJECT=${CTFY_PROJECT:-/root/workspace/sapper-c870444}
RUNS="$PROJECT/runs"
LOCK=${CTFY_LOCK_FILE:-/root/.config/ctfy/qualifier.lock}
SERVICE=${CTFY_SERVICE:-ctfy-qualifier.service}
STALE=${CTFY_HEARTBEAT_STALE:-900}
WINDOW_START=${CTFY_WINDOW_START:-1000}
WINDOW_END=${CTFY_WINDOW_END:-2005}

log() { printf '[watchdog] %s %s\n' "$(date -Is)" "$*"; }

now=$(date +%H%M)
if (( 10#$now < 10#$WINDOW_START || 10#$now > 10#$WINDOW_END )); then
  log "outside match window ($now)"
  exit 0
fi

lock_free() {
  flock -n "$LOCK" true 2>/dev/null
}

play_pid_for_lock() {
  local pid
  pid=$(lslocks 2>/dev/null \
    | awk -v f="$LOCK" '$0 ~ f {print $2; exit}')
  [[ -n "$pid" ]] || return 1
  tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q "lib/ctfy_local.py play" \
    || return 1
  printf '%s' "$pid"
}

stale_heartbeat() {
  local hb
  hb=$(ls -t "$RUNS"/*/heartbeat 2>/dev/null | head -1)
  [[ -n "$hb" ]] || return 1
  local age=$(( $(date +%s) - $(stat -c %Y "$hb") ))
  (( age > STALE ))
}

if lock_free; then
  log "no live campaign; starting $SERVICE"
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  systemctl start --no-block "$SERVICE"
  exit 0
fi

if stale_heartbeat; then
  pid=$(play_pid_for_lock || true)
  if [[ -n "${pid:-}" ]]; then
    log "heartbeat is stale; terminating hung play pid $pid"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 5
    kill -KILL "$pid" 2>/dev/null || true
    # Let the launcher's retry loop take over; start a fresh one if it is gone.
    if lock_free; then
      systemctl start --no-block "$SERVICE"
    fi
  else
    log "stale heartbeat but no verified play process; leaving state alone"
  fi
  exit 0
fi

log "campaign healthy"
