"""Meta-harness baseline arm: evolve a free-form agent module, no workflow DSL.

Faithful port of the meta-harness reference loop (stanford-iris-lab/meta-harness,
reference_examples/terminal_bench_2/meta_harness.py) onto the owf benchmarks:

  per iteration:  propose (coding agent edits agents/)  ->  validate (import gate)
                  ->  evaluate on train  ->  record evolution_summary + frontier.

Substrate: candidates run on the SAME frozen executor stack as the workflow arm
(pi agent loop, models registry, tool registry, Budget, Journal) through the
run-meta.ts entry — the arms differ ONLY in the representation of the evolvable
object (free JS module here; sandboxed workflow DSL there). The seed agent is
one runAgentNode call with the parity seed's exact parameters, so baseline
parity holds by construction.

Kept from the paper's protocol: append-only agents/ archive (hash-gated), one
candidate per iteration via pending_eval.json with hypothesis/prediction on
record, frontier_val.json + evolution_summary.jsonl as cross-iteration memory,
import gate before evaluation. Documented deviations: proposer is codex exec
with gpt-5.6-terra (meta-model parity across arms, instead of Claude Code); the
archive cold-starts from the parity seed only (no hand-written pattern library).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = ROOT / "executor"
SEED_DIR = Path(__file__).resolve().parent / "agents_seed"

from owf_bench.core.optimize import update_frontier, write_train_gold  # noqa: E402

DOMAIN_TOOLS = {"realmath": "['python']", "bcplus": "['search', 'open_doc']"}


def proposer_prompt(run_root: Path, domain: str, iteration: int, frontier: dict) -> str:
    pareto_lines = "\n".join(
        f"  - {p['name']}: score {p['score']:.4f}, {p['tokens']:,} tokens/task (report: {p['report']})"
        for p in frontier.get("pareto", [])
    ) or "  (empty)"
    return f"""# Meta-harness proposer — iteration {iteration} ({domain})

You evolve a free-form agent program for the {domain} benchmark. Run exactly one
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
  `results.jsonl`, `report.json`, and one rollout dir per task with
  `journal.jsonl` (every node, turn, tool call) plus full per-node transcripts
  (`node-*.jsonl`). Analyse failed and successful trajectories deeply before
  proposing.
- `evidence/train_gold.json` — train-set gold answers. Use them only to classify
  failure modes; never encode task-specific answers or gold-derived shortcuts
  into runtime behavior.
- `agents/` — every agent so far, baseline included. Read any, modify none.

## Edit scope

- CREATE a new file `agents/<candidate_name>.mjs` exporting
  `async function solve(task, core)`. Start from a copy of any existing agent or
  from scratch. Anything expressible in JS is in scope: control flow,
  decomposition, parallel calls (Promise.all), retries, verification loops,
  budget allocation, custom loop internals.
- The core surface (frozen):
  - `core.runAgentNode(prompt, opts, seq, core.deps)` — one full tool-using
    rollout. opts: {{system, model, tools, maxTurns (cap 200), temperature,
    thinkingLevel, schema, label, hooks}}. Returns {{result, status, turns,
    tokens}} (result is final text, or the schema-validated object, or null).
  - `core.deps` — frozen model/tool/journal/budget wiring; pass it through.
    `core.deps.budget.spentTokens()` style introspection is available.
  - `core.pi` — the pi-agent-core module, for candidates that rewrite the agent
    loop itself instead of using runAgentNode.
  - model must be 'deepseek-v4-flash' (the only SUT); tools for this domain:
    {DOMAIN_TOOLS[domain]}.
- Existing files under `agents/` and everything in the executor are READ-ONLY.
  The outer loop verifies this and rejects the iteration on violation.
- The candidate must be a GENERAL mechanism: no branching on task ids, no
  answer lookup tables, no scorer-specific strings, no information sources
  outside the benchmark tools, no bypassing token accounting.

## Quality bar

One mechanism per candidate — a hypothesis the evaluation can falsify. State in
the manifest what evidence motivated it and what result would refute it.
Validate before finishing (from {EXECUTOR}):
`npx tsx src/run-meta.ts --validate-agent {run_root}/agents/<candidate_name>.mjs`
Do not run the full evaluation.

## Required output

Write exactly `{run_root}/pending_eval.json`:

{{
  "iteration": {iteration},
  "candidates": [
    {{
      "name": "<snake_case_name>",
      "agent_file": "agents/<candidate_name>.mjs",
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
            for f in agents_dir.iterdir() if f.is_file()}


def propose(run_root: Path, prompt: str, model: str, iter_dir: Path, timeout: int) -> bool:
    out_file = iter_dir / "proposer_last_message.txt"
    proc = subprocess.run(
        ["codex", "exec", "-m", model, "-s", "workspace-write", "--skip-git-repo-check",
         "--color", "never", "-o", str(out_file), "-"],
        input=prompt, capture_output=True, text=True, timeout=timeout, cwd=run_root,
    )
    (iter_dir / "proposer_stderr.txt").write_text(proc.stderr[-8000:])
    return proc.returncode == 0


def validate_agent(agent_file: Path) -> tuple[bool, str]:
    proc = subprocess.run(["npx", "tsx", "src/run-meta.ts", "--validate-agent", str(agent_file)],
                          cwd=EXECUTOR, capture_output=True, text=True, timeout=120)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def evaluate_candidate(agent_file: Path, domain: str, eval_dir: Path, workers: int,
                       max_tokens: int, max_sec: int, limit: int | None) -> dict | None:
    cmd = ["python3", str(ROOT / "bench/owf_bench/core/runner.py"), "--domain", domain,
           "--agent-file", str(agent_file), "--subset", "train", "--workers", str(workers),
           "--out", str(eval_dir), "--max-tokens", str(max_tokens), "--max-wallclock-sec", str(max_sec)]
    if limit:
        cmd += ["--limit", str(limit)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=14400,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "bench")})
    report_path = eval_dir / "report.json"
    if not report_path.exists():
        print(f"  eval failed: {proc.stderr[-500:]}")
        return None
    return json.loads(report_path.read_text())


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
        base = os.environ.get("OWF_BCPLUS_SERVER", "http://127.0.0.1:8931")
        try:
            urllib.request.urlopen(f"{base}/search?q=ping&k=1", timeout=10)
        except Exception as e:
            raise SystemExit(f"bcplus corpus server not reachable at {base}: {e}")

    run_root = Path(args.run_root).resolve()
    agents_dir = run_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    baseline = f"baseline_{args.domain}.mjs"
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
        name = baseline.rsplit(".", 1)[0]
        point = {"name": name, "workflow": str(agents_dir / baseline),
                 "score": report["score"],
                 "tokens": report["tokens_per_task_total"],
                 "report": str(eval_dir / "report.json")}
        frontier = {"pareto": [point],
                    "per_task_best": {t: {"agent": name, "score": s}
                                      for t, s in report["task_scores"].items()}}
        frontier_path.write_text(json.dumps(frontier, indent=1))
        record({"iteration": 0, "name": name, "hypothesis": "parity seed",
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
        valid, msg = validate_agent(agent_file)
        if not valid:
            row.update({"rejected": f"validation failed: {msg[:500]}"})
            record(row)
            print(f"  REJECTED: {msg[:200]}")
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
