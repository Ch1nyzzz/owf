#!/bin/bash
# Held-out final: 200 sampled test tasks x 3 contemporaneous arms.
# Arms run sequentially at 64 workers each (concurrency policy: <128 total).
set -u
cd /data/home/yuhan/owf
IDS=$(python3 -c "import json; print(','.join(json.load(open('runs/opt_bcplus_v9/lab/heldout_sample_200.json'))['ids']))")
LOG=runs/opt_bcplus_v9/lab/heldout.log
echo "=== heldout start $(date -Is)" >> "$LOG"

PYTHONPATH=bench python3 bench/owf_bench/core/runner.py --domain bcplus \
  --workflow workflows/bcplus/seed_parity.js --subset test --task-ids "$IDS" \
  --repeats 1 --workers 64 --out runs/opt_bcplus_v9/lab/heldout_seed \
  --max-tokens 600000 --max-wallclock-sec 1800 >> "$LOG" 2>&1
echo "=== seed arm done $(date -Is)" >> "$LOG"

PYTHONPATH=bench python3 bench/owf_bench/core/runner.py --domain bcplus \
  --workflow runs/opt_bcplus_v8/iter_007/candidate.js --subset test --task-ids "$IDS" \
  --repeats 1 --workers 64 --out runs/opt_bcplus_v9/lab/heldout_v8champion \
  --max-tokens 600000 --max-wallclock-sec 1800 >> "$LOG" 2>&1
echo "=== v8 champion arm done $(date -Is)" >> "$LOG"

PYTHONPATH=bench python3 -m owf_bench.core.meta_delegate --opt-root runs/opt_bcplus_v9 \
  --out runs/opt_bcplus_v9/lab/heldout_meta --subset test --task-ids "$IDS" \
  --playbook runs/opt_bcplus_v9/lab/playbook_conservative.md \
  --repeats 1 --workers 64 --meta-concurrency 4 \
  --meta-base-url https://api.kimi.com/coding --meta-model kimi-k3 \
  --meta-key-env KIMI_API_KEY --meta-format anthropic >> "$LOG" 2>&1
echo "=== meta arm done $(date -Is)" >> "$LOG"
