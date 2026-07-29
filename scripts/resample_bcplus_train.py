"""Resample the bcplus train split to match the test distribution (2026-07-28).

Problem: the previous train (first 50 of the 249-task pool by id) contained ZERO
budget-blowup tasks (seed: 50/50 ok, 175k tokens/task) while the 780-task test is
30% budget_exceeded at 346k tokens/task. Turn caps and commitment rails tuned on
that train traded test-score symmetrically (rescued budget tasks, truncated long
completions) — the optimizer never saw the distribution it was graded on.

Fix: stratified re-draw of 50 tasks from the SAME 249-task pool (the only tasks
ever sanctioned for train), matching the test marginal over (budget_exceeded,
token-quartile-among-ok) as measured by the canonical seed rollouts on the old
substrate — the only per-task difficulty proxy that exists for every pool task:
  - pool tasks currently in test: runs/test_bcplus_seed/results.jsonl
  - pool tasks in the old train:  runs/bcplus_seed_train50_k1_b600k/results.jsonl
Both were measured under the same 600k budget. Unchosen pool tasks go (back) to
test; test size stays 780, membership shifts. Held-out gold never moves out of
data/, so no leak in either direction.

Deterministic: seeded RNG, provenance recorded in split.json.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 20260728
N_TRAIN = 50

split_path = ROOT / "data/bcplus/split.json"
split = json.loads(split_path.read_text())
old_train = list(split["train"])
old_test = list(split["test"])

# The sanctioned pool: the original 249-task train of the initial split (git 4a22537),
# verified to equal current train ∪ (199 pool tasks currently parked in test).
prov = split.get("_provenance", "")
orig = json.loads(Path(ROOT / "scripts/.bcplus_pool_orig.json").read_text())
pool = sorted(orig["train"])
assert len(pool) == 249 and set(old_train) <= set(pool)
moved_to_test = sorted(set(pool) - set(old_train))
assert len(moved_to_test) == 199 and set(moved_to_test) <= set(old_test)


def load_stats(path: str) -> dict[str, dict]:
    out = {}
    for line in (ROOT / path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        tok = r["tokens"]
        total = (tok["input"] + tok["output"]) if isinstance(tok, dict) else tok
        out[r["task_id"]] = {"tokens": total, "be": r["status"] == "budget_exceeded"}
    return out


test_stats = load_stats("runs/test_bcplus_seed/results.jsonl")
train_stats = load_stats("runs/bcplus_seed_train50_k1_b600k/results.jsonl")

stats = {**{t: test_stats[t] for t in moved_to_test},
         **{t: train_stats[t] for t in old_train}}
missing = [t for t in pool if t not in stats]
assert not missing, f"no seed stats for {missing[:5]}"

# Target marginal from the full 780-task test as the seed measured it.
target_be = sum(1 for s in test_stats.values() if s["be"]) / len(test_stats)
ok_tokens = sorted(s["tokens"] for s in test_stats.values() if not s["be"])
quart = [ok_tokens[len(ok_tokens) * i // 4] for i in (1, 2, 3)]


def bucket(s: dict) -> str:
    if s["be"]:
        return "be"
    t = s["tokens"]
    return "q1" if t < quart[0] else "q2" if t < quart[1] else "q3" if t < quart[2] else "q4"


n_be = round(N_TRAIN * target_be)
n_ok = N_TRAIN - n_be
want = {"be": n_be, **{q: n_ok // 4 for q in ("q1", "q2", "q3", "q4")}}
for q in ("q1", "q2", "q3", "q4")[: n_ok % 4]:
    want[q] += 1

by_bucket: dict[str, list[str]] = {}
for t in pool:
    by_bucket.setdefault(bucket(stats[t]), []).append(t)

rng = random.Random(SEED)
new_train: list[str] = []
short = {}
for b, n in want.items():
    avail = sorted(by_bucket.get(b, []))
    take = min(n, len(avail))
    new_train += rng.sample(avail, take)
    if take < n:
        short[b] = n - take
# Backfill any shortfall from the adjacent heaviest buckets, preserving realism.
if short:
    rest = sorted(set(pool) - set(new_train), key=lambda t: -stats[t]["tokens"])
    need = sum(short.values())
    new_train += rest[:need]

new_train = sorted(new_train)
assert len(new_train) == N_TRAIN
new_test = sorted((set(old_test) | set(old_train)) - set(new_train))
assert len(new_test) == 780, f"test size {len(new_test)}"

# Report the alignment before/after.
def dist(tasks, st):
    n = len(tasks)
    be = sum(1 for t in tasks if st[t]["be"]) / n
    toks = sum(st[t]["tokens"] for t in tasks) / n
    return f"be={be:.1%} mean_tokens={toks:,.0f}"

print("test-780 target:", dist(list(test_stats), test_stats))
print("old train-50:  ", dist(old_train, train_stats))
print("new train-50:  ", dist(new_train, stats))
print("new train buckets:", {b: sum(1 for t in new_train if bucket(stats[t]) == b)
                             for b in ("be", "q1", "q2", "q3", "q4")})
print("kept from old train:", len(set(new_train) & set(old_train)))

split_out = {
    "_provenance": prov + (
        f" | 2026-07-28 train resampled to match test distribution: stratified draw "
        f"(seed={SEED}) from the 249-task pool over (budget_exceeded, ok-token-quartile) "
        f"as measured by the canonical seed under the 600k budget; unchosen pool tasks "
        f"returned to test (test stays 780, membership shifted)."),
    "_moved_to_test": sorted(set(pool) - set(new_train)),
    "train": new_train,
    "test": new_test,
}
split_path.write_text(json.dumps(split_out, indent=1))
print(f"\nwrote {split_path}")
