#!/bin/bash
# Dual-arm bcplus optimization on the official DeepSeek API (2026-07-28).
#
# Chain: (1) re-measure the parity seed at k=3 on the new substrate — the switch from
# gpugeek to api.deepseek.com invalidates the old canonical iteration 0, and both arms
# must evolve from a seed measured on the substrate their candidates run on; (2) build
# the stability report (noise band); (3) launch both arms in parallel against the SAME
# baseline dir (shared iteration 0). Arms differ only by ORIENTATION_SWARM + the
# workflow-DSL representation (main) vs the paper-faithful free-JS proposer (meta).
#
# Workers: 64 per eval (user decision 2026-07-28) — thinking-on calls are slower,
# higher concurrency compensates; official API has held up under this load.
set -euo pipefail
cd /data/home/yuhan/owf
set -a; source .env; set +a
export PYTHONPATH=/data/home/yuhan/owf/bench

BASE=runs/bcplus_seed_official_think_trainv2_k1

if [ ! -f "$BASE/stability.json" ]; then
  echo "=== [1/3] seed baseline k=1 (user decision: measure once) on official API -> $BASE ==="
  python3 bench/owf_bench/core/runner.py --domain bcplus \
    --workflow workflows/bcplus/seed_parity.js --subset train --repeats 1 \
    --workers 64 --out "$BASE" --max-tokens 600000 --max-wallclock-sec 2700

  echo "=== [2/3] stability report ==="
  python3 bench/owf_bench/core/stability.py --run-dir "$BASE" --domain bcplus --subset train
fi

echo "=== [3/3] launching both arms ==="
python3 bench/owf_bench/core/optimize.py --domain bcplus \
  --opt-root runs/opt_bcplus_v8 --iters 10 \
  --seed-workflow workflows/bcplus/seed_parity.js --baseline-run "$BASE" \
  --eval-max-tokens 600000 --eval-max-sec 2700 --eval-workers 64 --proposer codex \
  > runs/opt_bcplus_v8.log 2>&1 &
MAIN_PID=$!
echo "main arm (opt_bcplus_v8) pid=$MAIN_PID"

python3 bench/owf_bench/metaharness_arm/meta_loop.py --domain bcplus \
  --run-root runs/metaharness_bcplus_v4 --baseline-run "$BASE" \
  --iterations 10 --workers 64 --max-tokens 600000 --max-sec 2700 \
  > runs/metaharness_bcplus_v4.log 2>&1 &
META_PID=$!
echo "meta arm (metaharness_bcplus_v4) pid=$META_PID"

wait "$MAIN_PID"; MAIN_RC=$?
wait "$META_PID"; META_RC=$?
echo "=== done: main rc=$MAIN_RC meta rc=$META_RC ==="
