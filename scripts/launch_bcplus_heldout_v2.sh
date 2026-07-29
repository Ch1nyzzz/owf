#!/bin/bash
# Held-out test (780 tasks) for the dual-arm bcplus round on the new substrate
# (official DeepSeek API, thinking on, new judge, resampled split). Sequential to
# keep API concurrency at 64. Reference seed first — every historical test number
# is void on this substrate, so the seed's test score is re-established here.
set -euo pipefail
cd /data/home/yuhan/owf
set -a; source .env; set +a
export PYTHONPATH=/data/home/yuhan/owf/bench

run() {  # name workflow
  echo "=== held-out: $1 ==="
  python3 bench/owf_bench/core/runner.py --domain bcplus \
    --workflow "$2" --subset test --workers 64 \
    --out "runs/$1" --max-tokens 600000 --max-wallclock-sec 2700
}

run test2_bcplus_seed        workflows/bcplus/seed_parity.js
run test2_bcplus_mainarm_v8  runs/opt_bcplus_v8/iter_007/candidate.js
echo "=== held-out: test2_bcplus_meta_v4 (agent-file) ==="
python3 bench/owf_bench/core/runner.py --domain bcplus \
  --agent-file runs/metaharness_bcplus_v4/agents/bcplus_structured_budget_54.mjs \
  --subset test --workers 64 \
  --out runs/test2_bcplus_meta_v4 --max-tokens 600000 --max-wallclock-sec 2700
echo "=== all held-out runs done ==="
