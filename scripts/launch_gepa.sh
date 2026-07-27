#!/usr/bin/env bash
# Launch the GEPA prompt-only baseline arm, detached (same rationale as launch_opt.sh:
# setsid + nohup so the run survives the calling shell).
#
# Usage:
#   scripts/launch_gepa.sh <domain> <out-dir> [extra run_gepa.py args...]
#
# Examples:
#   scripts/launch_gepa.sh realmath runs/gepa_realmath_v1
#   scripts/launch_gepa.sh bcplus   runs/gepa_bcplus_v1 --max-metric-calls 600

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DOMAIN="${1:-}"
OUT="${2:-}"
if [[ -z "$DOMAIN" || -z "$OUT" ]]; then
  sed -n '2,10p' "$0" >&2
  exit 2
fi
shift 2

# Canonical seed baselines: the shared iteration 0 across all arms (same dirs as
# launch_opt.sh). The seed candidate is served from this measurement, never re-rolled.
case "$DOMAIN" in
  realmath) BASELINE=runs/realmath_seed_train66_k1_b600k ;;
  bcplus)   BASELINE=runs/bcplus_seed_train50_k1_b600k ;;
  *) echo "unknown domain: $DOMAIN (expected realmath|bcplus)" >&2; exit 2 ;;
esac

[[ -f .env ]] || { echo ".env missing — the solver/judge keys live there" >&2; exit 1; }
[[ -d "$BASELINE" ]] || { echo "baseline run missing: $BASELINE" >&2; exit 1; }

RUNNING="$(pgrep -af "run_gepa\.py.*${OUT}" || true)"
if [[ -n "$RUNNING" ]]; then
  echo "already running on ${OUT}:" >&2
  echo "$RUNNING" >&2
  exit 1
fi

mkdir -p "$OUT"
LOG="$OUT/driver.log"

setsid nohup bash -c '
  set -a; source .env; set +a
  export PYTHONPATH=bench
  exec python3 -u bench/owf_bench/gepa_arm/run_gepa.py "$@"
' _ --domain "$DOMAIN" --out "$OUT" --baseline-run "$BASELINE" "$@" >> "$LOG" 2>&1 < /dev/null &

sleep 2
PID="$(pgrep -f "run_gepa\.py.*${OUT}" | head -1 || true)"
if [[ -z "$PID" ]]; then
  echo "driver failed to start — tail of $LOG:" >&2
  tail -5 "$LOG" >&2
  exit 1
fi
echo "gepa $DOMAIN started: pid=$PID"
echo "log:  tail -f $LOG"
echo "stop: kill $PID"
