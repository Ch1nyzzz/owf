#!/bin/bash
# Train exam v2: disciplined conservative playbook (thin-evidence rule), k=3.
# Contrast arm: same-day seed k=3 baseline runs/bcplus_seed_v9baseline_k3 (0.607).
set -u
cd /data/home/yuhan/owf
LOG=runs/opt_bcplus_v9/lab/train_exam_v2.log
echo "=== train exam v2 start $(date -Is)" >> "$LOG"

PYTHONPATH=bench python3 -m owf_bench.core.meta_delegate --opt-root runs/opt_bcplus_v9 \
  --out runs/opt_bcplus_v9/lab/meta_exam_k3c_v2 --subset train \
  --playbook runs/opt_bcplus_v9/lab/playbook_conservative.md \
  --repeats 3 --workers 64 --meta-concurrency 4 \
  --meta-base-url https://api.kimi.com/coding --meta-model kimi-k3 \
  --meta-key-env KIMI_API_KEY --meta-format anthropic >> "$LOG" 2>&1
echo "=== train exam v2 done $(date -Is)" >> "$LOG"
