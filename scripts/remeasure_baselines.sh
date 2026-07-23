#!/usr/bin/env bash
# Re-measure both seed baselines under the current token accounting.
#
# The old baselines recorded input tokens after pi had subtracted whatever
# cache-hit count gpugeek happened to report, so their cost is understated by an
# unknown amount and cannot seed the Pareto cost axis. The seed workflows, the
# model and the task splits are all unchanged — only the accounting is.
#
# k=1, not the original k=3: the frontier is bookkeeping measured at k=1 anyway,
# and the champion still owes a k=3 confirmation plus the held-out test set at the
# end. stability.json is copied from the old baseline because the noise band is a
# property of score repeatability, which token accounting does not touch.
#
# Both domains run concurrently at 32 workers each, keeping total API concurrency
# at the 64 already exercised by a single-domain eval.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; source .env; set +a
export PYTHONPATH=bench

RM_OUT=runs/realmath_seed_train66_k1_costaxis
BC_OUT=runs/bcplus_seed_train50_k1_costaxis

run_one() {
  local domain=$1 seed=$2 out=$3 max_tokens=$4; shift 4
  python3 bench/owf_bench/core/runner.py \
    --domain "$domain" --workflow "$seed" --subset train \
    --repeats 1 --workers 32 --out "$out" \
    --max-tokens "$max_tokens" --max-wallclock-sec 1800 "$@" \
    > "$out.log" 2>&1
}

mkdir -p "$RM_OUT" "$BC_OUT"
echo "=== baseline re-measure started $(date -Is) ==="

run_one realmath workflows/realmath/seed_parity.js "$RM_OUT" 300000 &
rm_pid=$!
run_one bcplus workflows/bcplus/seed_parity.js "$BC_OUT" 600000 --limit 50 &
bc_pid=$!

wait $rm_pid && echo "realmath done" || echo "realmath FAILED (see $RM_OUT.log)"
wait $bc_pid && echo "bcplus done"   || echo "bcplus FAILED (see $BC_OUT.log)"

# Noise band carries over: same seed, same model, same split — only accounting changed.
cp runs/realmath_seedv2_train66_k1/stability.json "$RM_OUT/stability.json"
cp runs/bcplus_seedv2_train50_k3/stability.json "$BC_OUT/stability.json"

echo "=== done $(date -Is) ==="
for d in "$RM_OUT" "$BC_OUT"; do
  python3 -c "
import json
r = json.load(open('$d/report.json'))
print(f\"  $d: score {r['score']:.4f} | {r['cost_per_task']:.4f} CNY/task | \"
      f\"{r['tokens_per_task']['input']} in + {r['tokens_per_task']['output']} out | {r['statuses']}\")
"
done
