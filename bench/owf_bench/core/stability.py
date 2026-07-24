"""Build the per-task stability report from a k-repeat baseline run.

The report is DATA for the optimizer's evidence surface (no advice attached):
pass sequences per task, stable-pass / stable-fail / oscillating sets, and the
run-level noise band (std of the k per-run macro scores).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def subset_ids(domain: str, subset: str) -> set[str]:
    return set(json.loads((ROOT / "data" / domain / "split.json").read_text())[subset])


def build(run_dir: Path, keep: set[str] | None = None) -> dict:
    """Stability over the tasks in `keep` (default: everything the run measured).

    The noise band is the std of the per-run macro scores, so it depends on how many
    tasks the mean is taken over: shrinking a train split widens it (each task carries
    more weight). Restricting an existing run to the new split therefore has to
    recompute this rather than inherit it.
    """
    results = [json.loads(l) for l in (run_dir / "results.jsonl").read_text().splitlines() if l.strip()]
    if keep is not None:
        missing = keep - {r["task_id"] for r in results}
        if missing:
            raise SystemExit(f"{run_dir} never measured {len(missing)} of the requested tasks, e.g. {sorted(missing)[:5]}")
        results = [r for r in results if r["task_id"] in keep]
    by_task: dict[str, dict[int, float]] = {}
    for r in results:
        by_task.setdefault(r["task_id"], {})[r["rep"]] = r["score"]
    k = max((len(v) for v in by_task.values()), default=0)

    seqs = {tid: [reps.get(i) for i in range(k)] for tid, reps in by_task.items()}
    stable_pass = sorted(t for t, s in seqs.items() if all(x == 1.0 for x in s if x is not None) and len(s) == k)
    stable_fail = sorted(t for t, s in seqs.items() if all(x == 0.0 for x in s if x is not None) and len(s) == k)
    oscillating = sorted(t for t in seqs if t not in stable_pass and t not in stable_fail)

    per_run_scores = []
    for i in range(k):
        vals = [seqs[t][i] for t in seqs if seqs[t][i] is not None]
        if vals:
            per_run_scores.append(sum(vals) / len(vals))
    mean = sum(per_run_scores) / len(per_run_scores) if per_run_scores else 0.0
    var = sum((x - mean) ** 2 for x in per_run_scores) / len(per_run_scores) if per_run_scores else 0.0

    # failure-cause strata for stable-fail tasks (data for the optimizer, no advice)
    causes: dict[str, list[str]] = {}
    for r in results:
        if r["task_id"] in set(stable_fail):
            causes.setdefault(r["task_id"], []).append(r.get("match_type", r.get("status", "?")))
    def bucket(cs: list[str]) -> str:
        n = {c: cs.count(c) for c in set(cs)}
        if n.get("no_answer", 0) * 2 >= len(cs):
            return "no_answer"
        if (n.get("judge_confirmed_mismatch", 0) + n.get("mismatch", 0)) * 2 >= len(cs):
            return "wrong_answer"
        if n.get("arity_mismatch", 0) * 2 >= len(cs):
            return "arity"
        return "mixed"
    stable_fail_causes = {t: bucket(cs) for t, cs in causes.items()}

    return {
        "k": k,
        "n_tasks": len(seqs),
        "per_run_scores": [round(x, 4) for x in per_run_scores],
        "mean_score": round(mean, 4),
        "noise_band": round(var ** 0.5, 4),
        "stable_pass": stable_pass,
        "stable_fail": stable_fail,
        "stable_fail_causes": stable_fail_causes,
        "stable_fail_cause_counts": {b: list(stable_fail_causes.values()).count(b) for b in set(stable_fail_causes.values())},
        "oscillating": oscillating,
        "counts": {"stable_pass": len(stable_pass), "stable_fail": len(stable_fail), "oscillating": len(oscillating)},
        "sequences": seqs,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--out", help="default: <run-dir>/stability.json")
    p.add_argument("--domain", help="restrict to a split subset of this domain (with --subset)")
    p.add_argument("--subset", default="train", help="train|test, used with --domain")
    args = p.parse_args()
    run_dir = Path(args.run_dir)
    keep = subset_ids(args.domain, args.subset) if args.domain else None
    report = build(run_dir, keep)
    out = Path(args.out) if args.out else run_dir / "stability.json"
    out.write_text(json.dumps(report, indent=1))
    print(json.dumps({key: report[key] for key in ("k", "n_tasks", "mean_score", "noise_band", "counts")}, indent=1))


if __name__ == "__main__":
    main()
