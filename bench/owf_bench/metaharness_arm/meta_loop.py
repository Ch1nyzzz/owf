"""Meta-harness baseline arm: evolve a free-form Python agent, no workflow DSL.

Faithful port of the meta-harness reference loop (stanford-iris-lab/meta-harness,
reference_examples/terminal_bench_2/meta_harness.py) onto the owf benchmarks:

  per iteration:  propose (coding agent edits agents/)  ->  validate (import gate)
                  ->  evaluate on train  ->  record evolution_summary + frontier.

Kept from the paper's protocol: append-only agents/ archive (new file per
candidate, existing files immutable), pending_eval.json contract with exactly
one candidate per iteration, hypothesis/changes on record, frontier + evolution
summary as the proposer's cross-iteration memory, trace access for diagnosis.

Documented deviations (docs/…, fairness to OUR matrix, not theirs):
  - proposer = codex exec with gpt-5.6-terra, not Claude Code — all three arms
    share the same meta model;
  - evaluation = owf train split with (score, tokens) Pareto bookkeeping and the
    same per-rollout budget as the other arms;
  - archive seeds from the parity baseline only (no hand-written pattern library).

What this arm is FOR: same evidence, same budget, same meta model as the main
arm — the only variable left is the representation of the optimization target
(free Python code here; explicit workflow DSL there).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SEED_DIR = Path(__file__).resolve().parent / "agents_seed"

from owf_bench.core.optimize import update_frontier, write_train_gold  # noqa: E402


def proposer_prompt(run_root: Path, domain: str, iteration: int, frontier: dict) -> str:
    pareto_lines = "\n".join(
        f"  - {p['name']}: score {p['score']:.4f}, {p['tokens']:,} tokens/task (report: {p['report']})"
        for p in frontier.get("pareto", [])
    ) or "  (empty)"
    return f"""# Meta-harness proposer — iteration {iteration} ({domain})

You evolve a Python agent harness for the {domain} benchmark. Run exactly one
iteration: analyse the evidence, form one falsifiable hypothesis, implement ONE
new candidate agent, and write the evaluation manifest. The outer loop
evaluates after you exit — do not run the full evaluation yourself.

## Objective

Push the (score up, tokens down) Pareto frontier. Tokens are input+output per
task. A candidate enters the frontier by not being dominated.

Current frontier:
{pareto_lines}

## Evidence in this workspace

- `frontier_val.json` — Pareto set and per-task best agents.
- `evolution_summary.jsonl` — every prior candidate: hypothesis, changes, score,
  tokens, statuses. Read this first; do not re-test a refuted hypothesis.
- `iter_*/eval/` — full evaluation artifacts per prior candidate: per-task
  `results.jsonl`, `report.json`, and per-task rollout dirs with
  `trajectory.jsonl` (every turn, tool call and result preview). Analyse failed
  and successful trajectories deeply before proposing.
- `evidence/train_gold.json` — train-set gold answers. Use them only to classify
  failure modes; never encode task-specific answers or gold-derived shortcuts
  into runtime behavior.
- `agents/` — every agent so far, baseline included. Read any, modify none.

## Edit scope

- CREATE a new file `agents/<candidate_name>.py` defining class `AgentHarness`.
  Subclass or import any existing agent, or write from scratch. Anything
  expressible in Python is in scope: control flow, decomposition, retries,
  verification, multiple LLM calls, budget allocation.
- The runner contract: `AgentHarness(client, log)` and
  `solve(task, deadline) -> answer`. `client.chat(messages, tools=...)` is the
  only model access; `client.spent()` reads the token budget consumed. `log(dict)`
  appends to the task trajectory. Tool primitives live in
  `owf_bench.metaharness_arm.harness_core.tools` (read-only): use DISPATCH or
  call run_python/run_search/run_open_doc directly.
- Existing files under `agents/`, and everything under
  `bench/owf_bench/metaharness_arm/harness_core/`, are READ-ONLY. The outer loop
  verifies this and rejects the iteration on violation.
- The candidate must be a GENERAL mechanism: no branching on task ids, no
  answer lookup tables, no scorer-specific strings.

## Quality bar

One mechanism per candidate — a hypothesis the evaluation can falsify. State in
the manifest what evidence motivated it and what result would refute it. Run a
quick import check (`PYTHONPATH={ROOT}/bench python3 -c "import ..."`) before
finishing; do not run the full evaluation.

## Required output

Write exactly `{run_root}/pending_eval.json`:

{{
  "iteration": {iteration},
  "candidates": [
    {{
      "name": "<snake_case_name>",
      "agent_file": "agents/<candidate_name>.py",
      "hypothesis": "<falsifiable claim grounded in cited evidence>",
      "changes": "<what the mechanism does, briefly>",
      "prediction": "<expected score/token effect and which tasks should move>"
    }}
  ]
}}

The candidates array must contain exactly one entry.
"""


def snapshot_hashes(agents_dir: Path) -> dict[str, str]:
    return {f.name: hashlib.sha1(f.read_bytes()).hexdigest()
            for f in agents_dir.glob("*.py")}


def propose(run_root: Path, prompt: str, model: str, iter_dir: Path, timeout: int) -> bool:
    out_file = iter_dir / "proposer_last_message.txt"
    proc = subprocess.run(
        ["codex", "exec", "-m", model, "-s", "workspace-write", "--skip-git-repo-check",
         "--color", "never", "-o", str(out_file), "-"],
        input=prompt, capture_output=True, text=True, timeout=timeout, cwd=run_root,
    )
    (iter_dir / "proposer_stderr.txt").write_text(proc.stderr[-8000:])
    return proc.returncode == 0


def evaluate_candidate(agent_file: Path, domain: str, eval_dir: Path, workers: int,
                       max_tokens: int, max_sec: int, limit: int | None) -> dict | None:
    from owf_bench.metaharness_arm.harness_core.runner import evaluate
    try:
        return evaluate(agent_file, domain, eval_dir, workers, max_tokens, max_sec, limit)
    except Exception as e:
        print(f"  eval failed: {e}")
        return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True, choices=["realmath", "bcplus"])
    p.add_argument("--run-root", required=True)
    p.add_argument("--iterations", type=int, default=10)
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--max-tokens", type=int, default=600_000)
    p.add_argument("--max-sec", type=int, default=1800)
    p.add_argument("--limit", type=int, help="cap train tasks (smoke runs)")
    p.add_argument("--proposer-model", default="gpt-5.6-terra")
    p.add_argument("--propose-timeout", type=int, default=5400)
    args = p.parse_args()

    if args.domain == "bcplus":  # fail fast, not 50 tasks deep
        import os
        base = os.environ.get("OWF_BCPLUS_SERVER", "http://127.0.0.1:8931")
        try:
            urllib.request.urlopen(f"{base}/search?q=ping&k=1", timeout=10)
        except Exception as e:
            raise SystemExit(f"bcplus corpus server not reachable at {base}: {e}")

    run_root = Path(args.run_root).resolve()
    agents_dir = run_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    baseline = f"baseline_{args.domain}.py"
    if not (agents_dir / baseline).exists():
        shutil.copy(SEED_DIR / baseline, agents_dir / baseline)
    write_train_gold(run_root, args.domain)  # same evidence parity as the main arm

    frontier_path = run_root / "frontier_val.json"
    summary_path = run_root / "evolution_summary.jsonl"

    def record(row: dict) -> None:
        with summary_path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if frontier_path.exists():
        frontier = json.loads(frontier_path.read_text())
    else:
        print("=== iteration 0: baseline evaluation ===")
        eval_dir = run_root / "iter_000/eval"
        report = evaluate_candidate(agents_dir / baseline, args.domain, eval_dir,
                                    args.workers, args.max_tokens, args.max_sec, args.limit)
        if report is None:
            raise SystemExit("baseline evaluation failed — nothing to evolve against")
        point = {"name": baseline[:-3], "workflow": str(agents_dir / baseline),
                 "score": report["score"], "tokens": report["tokens_per_task_total"],
                 "report": str(eval_dir / "report.json")}
        frontier = {"pareto": [point],
                    "per_task_best": {t: {"agent": baseline[:-3], "score": s}
                                      for t, s in report["task_scores"].items()}}
        frontier_path.write_text(json.dumps(frontier, indent=1))
        record({"iteration": 0, "name": baseline[:-3], "hypothesis": "parity seed",
                "changes": "baseline", "score": report["score"],
                "tokens_per_task_total": report["tokens_per_task_total"],
                "statuses": report["statuses"], "report": str(eval_dir / "report.json"),
                "entered_pareto": True})

    done = len([d for d in run_root.glob("iter_*") if (d / "eval/report.json").exists()])
    for it in range(done, done + args.iterations):
        print(f"=== iteration {it} (pareto: {len(frontier['pareto'])} pts) ===")
        iter_dir = run_root / f"iter_{it:03d}"
        iter_dir.mkdir(exist_ok=True)
        before = snapshot_hashes(agents_dir)
        pending = run_root / "pending_eval.json"
        pending.unlink(missing_ok=True)

        prompt = proposer_prompt(run_root, args.domain, it, frontier)
        (iter_dir / "proposer_prompt.md").write_text(prompt)
        t0 = time.time()
        ok = propose(run_root, prompt, args.proposer_model, iter_dir, args.propose_timeout)
        row: dict = {"iteration": it, "proposer_sec": round(time.time() - t0), "proposer_ok": ok}

        # Append-only gate: a proposer that edited history invalidates the iteration.
        tampered = [n for n, h in before.items()
                    if not (agents_dir / n).exists()
                    or hashlib.sha1((agents_dir / n).read_bytes()).hexdigest() != h]
        if tampered:
            for n in tampered:  # restore from the pristine seed if possible, else flag hard
                if (SEED_DIR / n).exists():
                    shutil.copy(SEED_DIR / n, agents_dir / n)
            row.update({"rejected": f"modified existing agents: {tampered}"})
            record(row)
            print(f"  REJECTED: modified existing agents {tampered}")
            continue

        if not pending.exists():
            row.update({"rejected": "no pending_eval.json"})
            record(row)
            print("  REJECTED: proposer wrote no pending_eval.json")
            continue
        manifest = json.loads(pending.read_text())
        cands = manifest.get("candidates") or []
        if len(cands) != 1:
            row.update({"rejected": f"expected exactly 1 candidate, got {len(cands)}"})
            record(row)
            continue
        cand = cands[0]
        agent_file = run_root / cand.get("agent_file", "")
        row.update({k: cand.get(k) for k in ("name", "hypothesis", "changes", "prediction")})
        if agents_dir not in agent_file.parents or not agent_file.exists():
            row.update({"rejected": f"agent_file outside agents/ or missing: {cand.get('agent_file')}"})
            record(row)
            continue
        try:  # import gate — the analogue of the main arm's write_workflow validation
            from owf_bench.metaharness_arm.harness_core.runner import load_agent_class
            load_agent_class(agent_file)
        except Exception as e:
            row.update({"rejected": f"import failed: {e!r}"})
            record(row)
            print(f"  REJECTED: import failed: {e!r}")
            continue

        report = evaluate_candidate(agent_file, args.domain, iter_dir / "eval",
                                    args.workers, args.max_tokens, args.max_sec, args.limit)
        if report is None:
            row.update({"eval_failed": True})
            record(row)
            continue
        point = {"name": cand["name"], "workflow": str(agent_file), "score": report["score"],
                 "tokens": report["tokens_per_task_total"], "report": str(iter_dir / "eval/report.json")}
        frontier["pareto"], entered = update_frontier(frontier["pareto"], point, 0.0)
        for t, s in report["task_scores"].items():
            best = frontier["per_task_best"].get(t)
            if best is None or s > best["score"]:
                frontier["per_task_best"][t] = {"agent": cand["name"], "score": s}
        frontier_path.write_text(json.dumps(frontier, indent=1))
        row.update({"score": report["score"], "tokens_per_task_total": report["tokens_per_task_total"],
                    "statuses": report["statuses"], "report": str(iter_dir / "eval/report.json"),
                    "entered_pareto": entered})
        record(row)
        verdict = "-> pareto" if entered else "dominated"
        print(f"  {cand['name']}: {report['score']:.3f} @ {report['tokens_per_task_total']:,} tok {verdict}")

    print(json.dumps(frontier["pareto"], indent=1))


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT / "bench"))
    from owf_bench.core.runner import load_dotenv
    load_dotenv()
    main()
