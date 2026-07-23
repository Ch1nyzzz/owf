#!/usr/bin/env bash
# Re-measure both seed baselines.
#
# realmath needs it: at 300k tokens its baseline lost 50/198 rollouts to budget or
# timeout (§六 基线诚实性 — a baseline that dies on its own cap measures the cap, not
# the model, and inflates every later gain). It now runs at 600k, alongside the 15s
# python timeout that stops a stuck sympy call from eating the whole wall clock.
#
# bcplus is re-measured too, for one reason only: to be scored under the same python
# timeout as the candidates it will be compared against. Its old baseline was already
# honest (1/150).
#
# k=1. The frontier is measured at k=1 anyway, and the champion still owes a k=3
# confirmation plus the held-out test set. stability.json carries over from the old
# baselines — the noise band is a property of score repeatability, unchanged here.
#
# Both domains run concurrently at 32 workers each, keeping total API concurrency at
# the 64 already exercised by a single-domain eval. python3 -u so the logs are
# readable while it runs rather than only at exit.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; source .env; set +a
export PYTHONPATH=bench

RM_OUT=runs/realmath_seed_train66_k1_b600k
BC_OUT=runs/bcplus_seed_train50_k1_b600k

run_one() {
  local domain=$1 seed=$2 out=$3 max_tokens=$4; shift 4
  python3 -u bench/owf_bench/core/runner.py \
    --domain "$domain" --workflow "$seed" --subset train \
    --repeats 1 --workers 32 --out "$out" \
    --max-tokens "$max_tokens" --max-wallclock-sec 1800 "$@" \
    > "$out.log" 2>&1
}

mkdir -p "$RM_OUT" "$BC_OUT"
echo "=== baseline re-measure started $(date -Is) ==="

run_one realmath workflows/realmath/seed_parity.js "$RM_OUT" 600000 &
rm_pid=$!
run_one bcplus workflows/bcplus/seed_parity.js "$BC_OUT" 600000 --limit 50 &
bc_pid=$!

wait $rm_pid && echo "realmath done" || echo "realmath FAILED (see $RM_OUT.log)"
wait $bc_pid && echo "bcplus done"   || echo "bcplus FAILED (see $BC_OUT.log)"

cp runs/realmath_seedv2_train66_k1/stability.json "$RM_OUT/stability.json"
cp runs/bcplus_seedv2_train50_k3/stability.json "$BC_OUT/stability.json"

echo "=== done $(date -Is) ==="
for d in "$RM_OUT" "$BC_OUT"; do
  python3 -c "
import json
r = json.load(open('$d/report.json'))
s = r['statuses']; n = sum(s.values())
dead = s.get('budget_exceeded', 0) + s.get('timeout', 0)
print(f\"  $d: score {r['score']:.4f} | {r['tokens_per_task_total']:,} tok/task | \"
      f\"resource deaths {dead}/{n} = {dead/n*100:.0f}% | {s}\")
"
done
