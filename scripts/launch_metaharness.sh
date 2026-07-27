#!/usr/bin/env bash
# Launch the meta-harness baseline arm, detached (setsid + nohup, same rationale
# as launch_opt.sh: the run must survive the calling shell).
#
# Usage:
#   scripts/launch_metaharness.sh <domain> <run-root> [extra meta_loop.py args...]
#
# Examples:
#   scripts/launch_metaharness.sh realmath runs/metaharness_realmath_v1
#   scripts/launch_metaharness.sh bcplus   runs/metaharness_bcplus_v1 --iterations 10

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DOMAIN="${1:-}"
RUN_ROOT="${2:-}"
if [[ -z "$DOMAIN" || -z "$RUN_ROOT" ]]; then
  sed -n '2,10p' "$0" >&2
  exit 2
fi
shift 2

[[ -f .env ]] || { echo ".env missing — the solver/judge keys live there" >&2; exit 1; }

RUNNING="$(pgrep -af "meta_loop\.py.*${RUN_ROOT}" || true)"
if [[ -n "$RUNNING" ]]; then
  echo "already running on ${RUN_ROOT}:" >&2
  echo "$RUNNING" >&2
  exit 1
fi

mkdir -p "$RUN_ROOT"
LOG="$RUN_ROOT/driver.log"

setsid nohup bash -c '
  set -a; source .env; set +a
  export PYTHONPATH=bench
  exec python3 -u bench/owf_bench/metaharness_arm/meta_loop.py "$@"
' _ --domain "$DOMAIN" --run-root "$RUN_ROOT" "$@" >> "$LOG" 2>&1 < /dev/null &

sleep 2
PID="$(pgrep -f "meta_loop\.py.*${RUN_ROOT}" | head -1 || true)"
if [[ -z "$PID" ]]; then
  echo "driver failed to start — tail of $LOG:" >&2
  tail -5 "$LOG" >&2
  exit 1
fi
echo "metaharness $DOMAIN started: pid=$PID"
echo "log:  tail -f $LOG"
echo "stop: kill $PID"
