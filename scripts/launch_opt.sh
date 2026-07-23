#!/usr/bin/env bash
# Launch an optimization driver detached from the calling shell.
#
# Usage:
#   scripts/launch_opt.sh <domain> <opt-root> [extra optimize.py args...]
#
# Examples:
#   scripts/launch_opt.sh realmath runs/opt_realmath_v3 --iters 8
#   scripts/launch_opt.sh bcplus   runs/opt_bcplus_v2   --iters 7
#
# Why setsid, and why this script exists at all: the drivers used to be started as
# plain background jobs from an interactive/agent shell, which left them in that
# shell's process group. On 2026-07-23 both runs died twice for exactly this reason
# — once when the background job was stopped (06:00), once when the session itself
# ended (09:50) — each time losing a half-finished optimizer round. setsid puts the
# driver in its own session with no controlling terminal, so SIGHUP and
# process-group kills cannot reach it.
#
# Resuming is automatic: optimize.py derives start_iter from state.json (last
# completed iter + 1), so re-running this after a crash continues the run. Note
# that a round only lands in state.json once its eval finishes, so a driver killed
# mid-eval will redo that round's optimizer.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DOMAIN="${1:-}"
OPT_ROOT="${2:-}"
if [[ -z "$DOMAIN" || -z "$OPT_ROOT" ]]; then
  sed -n '2,8p' "$0" >&2
  exit 2
fi
shift 2

# Per-domain constants. eval_max_tokens MUST equal the per-rollout budget the
# baseline was measured under: a candidate evaluated under a tighter cap is being
# compared against a frontier it was never allowed to reach. Keeping these here
# rather than in the command line is the point of the script.
case "$DOMAIN" in
  realmath)
    SEED=workflows/realmath/seed_parity.js
    BASELINE=runs/realmath_seedv2_train66_k1
    EVAL_ARGS=(--eval-max-tokens 300000)
    ;;
  bcplus)
    SEED=workflows/bcplus/seed_parity.js
    BASELINE=runs/bcplus_seedv2_train50_k3
    EVAL_ARGS=(--eval-max-tokens 600000 --eval-limit 50)
    ;;
  *)
    echo "unknown domain: $DOMAIN (expected realmath|bcplus)" >&2
    exit 2
    ;;
esac

[[ -f .env ]] || { echo ".env missing — the solver/judge keys live there" >&2; exit 1; }
[[ -d "$BASELINE" ]] || { echo "baseline run missing: $BASELINE" >&2; exit 1; }
[[ -f "$SEED" ]] || { echo "seed workflow missing: $SEED" >&2; exit 1; }

# §六 (基线诚实性): a baseline that dies on its own budget measures the cap, not the
# model, and every later gain is inflated by however much of that cap the candidate
# merely unlocked. Warn rather than block — the threshold is a judgement call, and the
# run is still meaningful if you know the floor is soft.
DEATHS="$(python3 -c "
import json,sys
r=json.load(open('$BASELINE/report.json'))
s=r['statuses']; n=sum(s.values())
d=s.get('budget_exceeded',0)+s.get('timeout',0)
print(f'{d} {n} {d/n*100:.0f}')
" 2>/dev/null || echo "")"
if [[ -n "$DEATHS" ]]; then
  read -r d n pct <<<"$DEATHS"
  (( pct >= 10 )) && {
    echo "WARNING: baseline $BASELINE lost $d/$n rollouts ($pct%) to budget/timeout." >&2
    echo "         Gains measured against it will be partly 'candidate got more resources'." >&2
    echo "         Consider raising --eval-max-tokens and re-measuring the baseline first." >&2
  }
fi

# Refuse to double-start. Two drivers on one opt-root would interleave iter_NNN
# directories and race on state.json; an eval still running from a previous resume
# counts too, since its round is not yet recorded.
RUNNING="$(pgrep -af "(optimize|runner)\.py.*${OPT_ROOT}" || true)"
if [[ -n "$RUNNING" ]]; then
  echo "already working on ${OPT_ROOT}:" >&2
  echo "$RUNNING" >&2
  exit 1
fi

mkdir -p "$OPT_ROOT"
LOG="$OPT_ROOT/driver.log"

# bash -c '...' _ ARGS... so every argument survives as a real argv entry.
# < /dev/null detaches stdin; nohup blocks SIGHUP; setsid gives it a new session.
setsid nohup bash -c '
  set -a; source .env; set +a
  export PYTHONPATH=bench
  exec python3 -u bench/owf_bench/core/optimize.py "$@"
' _ --domain "$DOMAIN" --opt-root "$OPT_ROOT" \
    --seed-workflow "$SEED" --baseline-run "$BASELINE" \
    "${EVAL_ARGS[@]}" \
    --eval-workers 64 --opt-max-tokens 8000000 --opt-max-sec 9000 \
    "$@" >> "$LOG" 2>&1 < /dev/null &

sleep 2
PID="$(pgrep -f "optimize\.py.*${OPT_ROOT}" | head -1 || true)"
if [[ -z "$PID" ]]; then
  echo "driver failed to start — tail of $LOG:" >&2
  tail -5 "$LOG" >&2
  exit 1
fi

# sid == pid means the driver leads its own session: the detach worked.
SID="$(ps -o sid= -p "$PID" | tr -d ' ')"
echo "$DOMAIN driver started: pid=$PID sid=$SID  (this shell's sid: $(ps -o sid= -p $$ | tr -d ' '))"
[[ "$PID" == "$SID" ]] || echo "WARNING: sid != pid — the driver is NOT detached" >&2
echo "log:  tail -f $LOG"
echo "stop: kill $PID"
