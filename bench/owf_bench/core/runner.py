"""Generic evaluation runner: workflow.js x task set -> (score, tokens) report.

For each task (x k repeats): write a task.json WITHOUT gold (the workflow never
sees the answer), spawn the Node executor, read result.json + journal, grade via
the domain grader, aggregate. Infra errors (non-zero executor exit) are retried;
candidate failures (workflow_error etc.) score 0.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = ROOT / "executor"
INFRA_RETRIES = 2


def load_dotenv() -> None:
    """Load ROOT/.env without overriding existing env (graders need judge keys)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    import os, re

    for line in env_file.read_text().splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if m and os.environ.get(m.group(1)) is None and m.group(2):
            os.environ[m.group(1)] = m.group(2)


def load_tasks(domain: str, subset: str, limit: int | None, task_ids: list[str] | None = None) -> list[dict]:
    """Tasks for one eval. `task_ids` selects specific tasks; `limit` takes a prefix.

    Without task_ids a probe can only ever see the same first N ids, which is how the
    optimizer ended up shipping candidates whose target task it had no way to test.
    Selection still happens strictly inside `subset`, so the test split stays sealed.
    """
    data_dir = ROOT / "data" / domain
    tasks = [json.loads(l) for l in (data_dir / "tasks.jsonl").read_text().splitlines() if l.strip()]
    split = json.loads((data_dir / "split.json").read_text())
    if subset in ("train", "test"):
        wanted = set(split[subset])
        tasks = [t for t in tasks if t["id"] in wanted]
    elif subset != "all":
        raise SystemExit(f"unknown subset: {subset}")
    tasks.sort(key=lambda t: t["id"])
    if task_ids:
        by_id = {t["id"]: t for t in tasks}
        missing = [tid for tid in task_ids if tid not in by_id]
        if missing:
            raise SystemExit(f"task ids not in {domain}/{subset}: {missing}")
        return [by_id[tid] for tid in task_ids]
    return tasks[:limit] if limit else tasks


def run_one(task: dict, workflow: Path, domain: str, out_dir: Path, rep: int, max_tokens: int, max_sec: int,
            agent_file: Path | None = None) -> dict:
    run_dir = out_dir / f"{task['id']}__r{rep}"
    run_dir.mkdir(parents=True, exist_ok=True)
    public = {k: v for k, v in task.items() if k not in ("gold", "judge_system", "judge_template")}
    task_file = run_dir / "task.json"
    task_file.write_text(json.dumps(public, ensure_ascii=False))

    # meta-harness arm: same runner, same stack, different entry — the candidate is a
    # free-form agent module (run-meta.ts) instead of a sandboxed workflow (run.ts).
    entry = (["src/run-meta.ts", "--agent", str(agent_file)] if agent_file
             else ["src/run.ts", "--workflow", str(workflow)])

    # resume: an existing verdict means the rollout is done; only grading re-runs
    if not (run_dir / "result.json").exists():
        for attempt in range(INFRA_RETRIES + 1):
            proc = subprocess.run(
                [
                    "npx", "tsx", *entry,
                    "--task", str(task_file),
                    "--out", str(run_dir),
                    "--domain", domain,
                    "--max-tokens", str(max_tokens),
                    "--max-wallclock-sec", str(max_sec),
                ],
                cwd=EXECUTOR,
                capture_output=True,
                text=True,
                timeout=max_sec + 120,
            )
            if proc.returncode == 0:
                break
            if attempt == INFRA_RETRIES:
                return {"task_id": task["id"], "rep": rep, "status": "infra_error", "score": 0.0, "tokens": {"input": 0, "output": 0}, "error": proc.stderr[-500:]}
            time.sleep(5)

    summary = json.loads((run_dir / "result.json").read_text())
    grader = importlib.import_module(f"owf_bench.{domain}.grade")
    answer = None
    if isinstance(summary.get("result"), dict):
        answer = summary["result"].get("answer")
    elif summary.get("result") is not None:
        answer = summary["result"]
    if hasattr(grader, "grade_task"):
        correct, match_type = grader.grade_task(answer, task)
    else:
        correct, match_type = grader.grade(answer, task["gold"])
    return {
        "task_id": task["id"],
        "rep": rep,
        "status": summary["status"],
        "score": 1.0 if correct else 0.0,
        "match_type": match_type,
        "tokens": summary["totalTokens"],
        "durationMs": summary["durationMs"],
        "answerPreview": str(answer)[:200],
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True)
    p.add_argument("--workflow", help="workflow.js candidate (run.ts entry)")
    p.add_argument("--agent-file", help="meta-harness agent module (run-meta.ts entry)")
    p.add_argument("--subset", default="train", help="train|test|all")
    p.add_argument("--limit", type=int)
    p.add_argument("--task-ids", help="comma-separated task ids (within --subset); overrides --limit")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", required=True)
    p.add_argument("--max-tokens", type=int, default=400_000)
    p.add_argument("--max-wallclock-sec", type=int, default=900)
    args = p.parse_args()

    ids = [t.strip() for t in args.task_ids.split(",") if t.strip()] if args.task_ids else None
    tasks = load_tasks(args.domain, args.subset, args.limit, ids)
    if bool(args.workflow) == bool(args.agent_file):
        raise SystemExit("exactly one of --workflow / --agent-file is required")
    workflow = Path(args.workflow).resolve() if args.workflow else Path(args.agent_file).resolve()
    agent_file = Path(args.agent_file).resolve() if args.agent_file else None
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(t, rep) for t in tasks for rep in range(args.repeats)]
    print(f"{args.domain}/{args.subset}: {len(tasks)} tasks x {args.repeats} repeats = {len(jobs)} runs")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, t, workflow, args.domain, out_dir, rep, args.max_tokens, args.max_wallclock_sec, agent_file): (t["id"], rep) for t, rep in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            print(f"[{i}/{len(jobs)}] {r['task_id']} r{r['rep']} -> {r['score']:.0f} ({r.get('match_type', r['status'])}) tok={r['tokens']['input']}+{r['tokens']['output']}")

    results.sort(key=lambda r: (r["task_id"], r["rep"]))
    with (out_dir / "results.jsonl").open("w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # per-task mean over repeats, then macro average
    by_task: dict[str, list[dict]] = {}
    for r in results:
        by_task.setdefault(r["task_id"], []).append(r)
    task_scores = {tid: sum(x["score"] for x in rs) / len(rs) for tid, rs in by_task.items()}
    total_in = sum(r["tokens"]["input"] for r in results)
    total_out = sum(r["tokens"]["output"] for r in results)
    report = {
        "workflow": str(workflow),
        "domain": args.domain,
        "subset": args.subset,
        "n_tasks": len(by_task),
        "repeats": args.repeats,
        "score": sum(task_scores.values()) / len(task_scores) if task_scores else 0.0,
        "tokens_total": {"input": total_in, "output": total_out},
        "tokens_per_task": {"input": total_in // max(1, len(results)), "output": total_out // max(1, len(results))},
        # Pareto's second axis: total tokens per task. Tokens rather than money because
        # gpugeek publishes a single input rate and documents no cache pricing, so any
        # CNY figure would rest on an unsourceable assumption.
        "tokens_per_task_total": (total_in + total_out) // max(1, len(results)),
        "statuses": {s: sum(1 for r in results if r["status"] == s) for s in {r["status"] for r in results}},
        "task_scores": task_scores,
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ("score", "n_tasks", "tokens_per_task", "tokens_per_task_total", "statuses")}, indent=1))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "bench"))
    load_dotenv()
    main()
