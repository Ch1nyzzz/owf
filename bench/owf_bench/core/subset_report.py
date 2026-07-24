"""Restrict an existing eval report to a subset of its tasks.

Shrinking a train split does not need the seed re-run: results.jsonl already holds
the per-task score and token counts, so the aggregate can be recomputed exactly over
whichever tasks remain. Recomputing beats re-running — same numbers, no API spend,
and the retained tasks keep the identical rollouts the optimizer will read as evidence.

Aggregation mirrors runner.py: macro-average of per-task means, tokens averaged over
rollouts (not tasks), so a k>1 run stays consistent with how it was originally scored.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def rebuild(run_dir: Path, keep: set[str]) -> dict:
    results = [json.loads(l) for l in (run_dir / "results.jsonl").read_text().splitlines() if l.strip()]
    missing = keep - {r["task_id"] for r in results}
    if missing:
        raise SystemExit(f"{run_dir} never measured {len(missing)} requested tasks, e.g. {sorted(missing)[:5]}")
    kept = [r for r in results if r["task_id"] in keep]

    by_task: dict[str, list[dict]] = {}
    for r in kept:
        by_task.setdefault(r["task_id"], []).append(r)
    task_scores = {t: sum(x["score"] for x in rs) / len(rs) for t, rs in by_task.items()}
    total_in = sum(r["tokens"]["input"] for r in kept)
    total_out = sum(r["tokens"]["output"] for r in kept)

    old = json.loads((run_dir / "report.json").read_text())
    return {
        **{k: old[k] for k in ("workflow", "domain", "subset") if k in old},
        "n_tasks": len(by_task),
        "repeats": old.get("repeats", 1),
        "score": sum(task_scores.values()) / len(task_scores) if task_scores else 0.0,
        "tokens_total": {"input": total_in, "output": total_out},
        "tokens_per_task": {"input": total_in // max(1, len(kept)), "output": total_out // max(1, len(kept))},
        "tokens_per_task_total": (total_in + total_out) // max(1, len(kept)),
        "statuses": {s: sum(1 for r in kept if r["status"] == s) for s in {r["status"] for r in kept}},
        "task_scores": task_scores,
        "_subset_of": str(run_dir / "report.json"),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--domain", required=True)
    p.add_argument("--subset", default="train")
    p.add_argument("--out", help="default: <run-dir>/report.json (originals saved as *.full.json)")
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    keep = set(json.loads((ROOT / "data" / args.domain / "split.json").read_text())[args.subset])
    report = rebuild(run_dir, keep)

    out = Path(args.out) if args.out else run_dir / "report.json"
    if not args.out:
        backup = run_dir / "report.full.json"
        if not backup.exists():
            backup.write_text((run_dir / "report.json").read_text())
            print(f"  original kept at {backup}")
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ("n_tasks", "score", "tokens_per_task_total", "statuses")}, indent=1))


if __name__ == "__main__":
    main()
