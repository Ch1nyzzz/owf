"""Evaluation runner for meta-harness candidates — READ-ONLY for the proposer.

Mirrors the semantics of owf_bench/core/runner.py (the workflow arm's runner):
public task payload without gold, per-task token/wallclock budget, per-task
trajectory on disk, same graders, same report fields. The proposer analyses
these artifacts; it never runs this module itself (the outer loop does).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def load_agent_class(agent_file: Path):
    spec = importlib.util.spec_from_file_location(f"candidate_{agent_file.stem}", agent_file)
    module = importlib.util.module_from_spec(spec)
    # Registering the module lets a candidate subclass another candidate via import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.AgentHarness


def run_one(agent_cls, task: dict, domain: str, out_dir: Path, max_tokens: int, max_sec: int) -> dict:
    from owf_bench.metaharness_arm.harness_core.client import FlashClient

    run_dir = out_dir / f"{task['id']}__r0"
    run_dir.mkdir(parents=True, exist_ok=True)
    public = {k: v for k, v in task.items() if k not in ("gold", "judge_system", "judge_template")}
    (run_dir / "task.json").write_text(json.dumps(public, ensure_ascii=False))

    trajectory_path = run_dir / "trajectory.jsonl"
    traj_file = trajectory_path.open("w")

    def log(event: dict) -> None:
        traj_file.write(json.dumps({"ts": time.time(), **event}, ensure_ascii=False) + "\n")
        traj_file.flush()

    client = FlashClient(token_budget=max_tokens)
    t0 = time.time()
    status, answer = "ok", None
    try:
        agent = agent_cls(client, log)
        answer = agent.solve(public, deadline=t0 + max_sec)
        if answer is None:
            status = "no_answer"
    except Exception as e:  # candidate bug: score 0, cause on record, never crash the eval
        status = "agent_error"
        log({"error": repr(e)})
    finally:
        traj_file.close()

    import importlib as _importlib
    grader = _importlib.import_module(f"owf_bench.{domain}.grade")
    correct, match_type = grader.grade_task(answer, task)
    summary = {
        "status": status,
        "result": {"answer": answer},
        "totalTokens": dict(client.tokens),
        "durationMs": int((time.time() - t0) * 1000),
    }
    (run_dir / "result.json").write_text(json.dumps(summary, ensure_ascii=False))
    return {
        "task_id": task["id"], "rep": 0, "status": status,
        "score": 1.0 if correct else 0.0, "match_type": match_type,
        "tokens": dict(client.tokens), "durationMs": summary["durationMs"],
        "answerPreview": str(answer)[:200],
    }


def evaluate(agent_file: Path, domain: str, out_dir: Path, workers: int,
             max_tokens: int, max_sec: int, limit: int | None = None) -> dict:
    from owf_bench.core.runner import load_tasks  # same split discipline as the main arm

    agent_cls = load_agent_class(agent_file)
    tasks = load_tasks(domain, "train", limit)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_one, agent_cls, t, domain, out_dir, max_tokens, max_sec): t["id"]
                   for t in tasks}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            print(f"[{i}/{len(tasks)}] {r['task_id']} -> {r['score']:.0f} ({r['match_type']}) "
                  f"tok={r['tokens']['input']}+{r['tokens']['output']}", flush=True)

    results.sort(key=lambda r: r["task_id"])
    with (out_dir / "results.jsonl").open("w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    total_in = sum(r["tokens"]["input"] for r in results)
    total_out = sum(r["tokens"]["output"] for r in results)
    task_scores = {r["task_id"]: r["score"] for r in results}
    report = {
        "agent": str(agent_file), "domain": domain, "n_tasks": len(results),
        "score": sum(task_scores.values()) / len(task_scores) if task_scores else 0.0,
        "tokens_per_task": {"input": total_in // max(1, len(results)), "output": total_out // max(1, len(results))},
        "tokens_per_task_total": (total_in + total_out) // max(1, len(results)),
        "statuses": {s: sum(1 for r in results if r["status"] == s) for s in {r["status"] for r in results}},
        "task_scores": task_scores,
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agent-file", required=True)
    p.add_argument("--domain", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--max-tokens", type=int, default=600_000)
    p.add_argument("--max-sec", type=int, default=1800)
    p.add_argument("--limit", type=int)
    args = p.parse_args()
    report = evaluate(Path(args.agent_file), args.domain, Path(args.out), args.workers,
                      args.max_tokens, args.max_sec, args.limit)
    print(json.dumps({k: report[k] for k in ("score", "n_tasks", "tokens_per_task_total", "statuses")}, indent=1))


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "bench"))
    from owf_bench.core.runner import load_dotenv
    load_dotenv()
    main()
