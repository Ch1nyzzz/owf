"""Meta-harness baseline arm: evolve a free-form agent module, no workflow DSL.

Loop protocol from the meta-harness reference (stanford-iris-lab/meta-harness):
append-only agents/ archive (hash-gated), one candidate per iteration via
pending_eval.json with hypothesis/prediction on record, frontier_val.json +
evolution_summary.jsonl as cross-iteration memory, import gate before
evaluation.

Optimizer organization: the SAME single codex session per iteration as the
main arm (core/codex_solo.py) — which is also the original meta-harness
loop's shape. The organization is a controlled variable of the arm
comparison; the arms differ ONLY in the representation of the evolvable
object (free JS module here, workflow DSL there). Substrate is likewise shared: candidates run through run-meta.ts on the
executor's frozen stack, and iteration 0 is the canonical seed measurement.

Candidate runtime has no sandbox (free-code evolution is the point), so the
cheating boundary gets a mechanical tripwire instead: a static scan rejects
candidates that reach for fs/network/process or the repo's data/ (held-out
gold). It is a tripwire, not a proof — the append-only archive stays auditable
and the champion is reviewed before certification.
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

from owf_bench.core.codex_solo import ArmSpec, run_solo  # noqa: E402
from owf_bench.core.optimize import EXPLORE_HINT_ROUNDS, update_frontier, write_train_gold  # noqa: E402

DOMAIN_TOOLS = {"realmath": "['python']", "bcplus": "['search', 'open_doc']"}

# Tripwire for the unsandboxed candidate runtime. Catches the obvious reaches for
# ambient authority; the archive audit catches the subtle ones.
FORBIDDEN_PATTERNS = [
    "node:fs", "node:child_process", "node:net", "node:http", "node:https", "node:worker_threads",
    "child_process", "require(", "fetch(", "XMLHttpRequest", "WebSocket",
    "process.env", "process.exit", "readFileSync", "writeFileSync",
    "data/realmath", "data/bcplus", "tasks.jsonl", "split.json",
]


def static_scan(agent_file: Path) -> list[str]:
    src = agent_file.read_text()
    return [p for p in FORBIDDEN_PATTERNS if p in src]


def rep_contract(domain: str, agents_dir: Path) -> str:
    return f"""THE REPRESENTATION — a free-form agent module (plain JS, no workflow DSL):
- CREATE a new file agents/<candidate_name>.mjs exporting `async function solve(task, core)`.
  Start from a copy of any existing agent (import/subclass allowed) or from scratch. Anything
  expressible in JS is in scope: control flow, decomposition, parallel calls (Promise.all),
  retries, verification loops, budget allocation, custom loop internals.
- The core surface (frozen):
  - core.runAgentNode(prompt, opts, seq, core.deps) — one full tool-using rollout.
    opts: {{system, model, tools, maxTurns (cap 200), temperature, thinkingLevel, schema, label,
    hooks}}. Returns {{result, status, turns, tokens}} (final text, or the schema-validated
    object, or null on failure).
  - core.deps — frozen model/tool/journal/budget wiring; pass it through.
  - core.pi — the pi-agent-core module, for candidates that rewrite the agent loop itself.
  - model must be 'deepseek-v4-flash' (the only SUT); tools for this domain: {DOMAIN_TOOLS[domain]}.
- Existing files under agents/ and everything in the executor are READ-ONLY; the outer loop
  verifies this and rejects the iteration on violation.
- Hard boundary: the candidate must be a GENERAL mechanism — no branching on task ids, no
  answer lookup tables, no scorer-specific strings. No filesystem, network, process or
  environment access from candidate code (a static scan rejects it). The repo's data/
  directory is OFF-LIMITS — held-out gold lives there; evidence/train_gold.json is the only
  sanctioned gold channel."""


def evidence_text(run_root: Path, domain: str, it: int, frontier: dict, stalled: int,
                  first_round: bool = False) -> str:
    pareto_lines = "\n".join(
        f"  - {p['name']}: score {p['score']:.4f}, {p['tokens']:,} tokens/task (report: {p['report']})"
        for p in frontier.get("pareto", [])
    ) or "  (empty)"
    gold = run_root / "evidence/train_gold.json"
    gold_line = (
        f"Train-set gold answers: {gold} (task_id -> gold) — evidence for judging what a rollout "
        "actually produced against what was required.\n"
    ) if gold.exists() else ""
    stalled_line = (
        f"The frontier has not moved in the last {stalled} rounds — refinement has stopped paying; "
        f"weigh a redesign.\n"
    ) if stalled >= EXPLORE_HINT_ROUNDS else ""
    first_round_line = (
        "This is the first round: ship a REDESIGN — design the organization for this task from "
        "its anatomy. Refinement has its turn once there is a designed organization to refine.\n"
    ) if first_round else ""
    return (
        f"Optimization round {it} for domain '{domain}'. Working directory: {run_root} (the run root).\n\n"
        f"CURRENT PARETO FRONTIER on (score up, tokens down):\n{pareto_lines}\n"
        "A candidate enters the frontier by not being dominated: it must beat some point on score or use "
        "fewer tokens, without being worse on the other axis. Admission is exact. Tokens are input+output "
        "per task.\n"
        f"{gold_line}"
        f"{first_round_line}"
        f"{stalled_line}"
        "Evidence layout: evolution_summary.jsonl — every prior candidate: hypothesis, changes, score, "
        "tokens, statuses; read it first and do not re-test a refuted hypothesis. frontier_val.json — "
        "Pareto set and per-task best agents. iter_*/eval/ — full evaluation artifacts per prior "
        "candidate (results.jsonl, report.json, one rollout dir per task with journal.jsonl and full "
        "per-node transcripts). The canonical seed rollouts are the baseline evaluation referenced by "
        "iteration 0 in evolution_summary.jsonl. agents/ — every agent so far; read any, modify none. "
        "NOTES.md — cross-round memory."
    )


def meta_stalled_rounds(summary_path: Path) -> int:
    if not summary_path.exists():
        return 0
    rows = [json.loads(l) for l in summary_path.read_text().splitlines() if l.strip()]
    n = 0
    for row in reversed(rows):
        if row.get("entered_pareto"):
            break
        n += 1
    return n


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


def snapshot_hashes(agents_dir: Path) -> dict[str, str]:
    return {f.name: hashlib.sha1(f.read_bytes()).hexdigest()
            for f in agents_dir.iterdir() if f.is_file()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True, choices=["realmath", "bcplus"])
    p.add_argument("--run-root", required=True)
    p.add_argument("--baseline-run", required=True,
                   help="canonical seed eval dir (report.json) — the shared iteration 0 for every arm")
    p.add_argument("--iterations", type=int, default=10)
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--max-tokens", type=int, default=600_000)
    p.add_argument("--max-sec", type=int, default=1800)
    p.add_argument("--limit", type=int, help="cap train tasks (smoke runs)")
    p.add_argument("--proposer-model", default="gpt-5.6-terra")
    p.add_argument("--lead-timeout", type=int, default=5400)
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
        # Shared iteration 0: every arm evolves from the SAME canonical seed measurement.
        base = Path(args.baseline_run).resolve()
        report = json.loads((base / "report.json").read_text())
        base_tokens = report.get("tokens_per_task_total")
        if base_tokens is None:
            t = report["tokens_per_task"]
            base_tokens = t["input"] + t["output"]
        name = baseline.rsplit(".", 1)[0]
        point = {"name": name, "workflow": str(agents_dir / baseline),
                 "score": report["score"], "tokens": base_tokens,
                 "report": str(base / "report.json")}
        frontier = {"pareto": [point],
                    "per_task_best": {t: {"agent": name, "score": s}
                                      for t, s in report["task_scores"].items()}}
        frontier_path.write_text(json.dumps(frontier, indent=1))
        record({"iteration": 0, "name": name, "hypothesis": "parity seed",
                "changes": f"canonical baseline (shared iteration 0): {base}",
                "score": report["score"], "tokens_per_task_total": base_tokens,
                "statuses": report.get("statuses"), "report": str(base / "report.json"),
                "entered_pareto": True})

    done = len([d for d in run_root.glob("iter_*") if (d / "eval/report.json").exists()])
    for it in range(done, done + args.iterations):
        print(f"=== iteration {it} (pareto: {len(frontier['pareto'])} pts) ===")
        iter_dir = run_root / f"iter_{it:03d}"
        iter_dir.mkdir(exist_ok=True)
        before = snapshot_hashes(agents_dir)
        pending = run_root / "pending_eval.json"
        pending.unlink(missing_ok=True)

        spec = ArmSpec(
            subject="a free-form agent program (a plain-JS agent module)",
            rep_contract=rep_contract(args.domain, agents_dir),
            evidence_text=evidence_text(run_root, args.domain, it, frontier,
                                        meta_stalled_rounds(summary_path),
                                        first_round=(it == done and done == 0)),
            candidate_path=Path(f"{agents_dir}/<candidate_name>.mjs (a NEW file; you choose the name)"),
            validate_cmd=f"cd {EXECUTOR} && npx tsx src/run-meta.ts --validate-agent <your file>",
            extra=(
                f"- Also write exactly {pending}:\n"
                '  {"iteration": N, "candidates": [{"name": "<snake_case_name>", '
                '"agent_file": "agents/<candidate_name>.mjs", "hypothesis": "<falsifiable claim '
                'grounded in cited evidence>", "changes": "<what the mechanism does>", '
                '"prediction": "<expected score/token effect and which tasks should move>"}]}\n'
                "  The candidates array must contain exactly one entry."
            ),
        )
        t0 = time.time()
        sessions = run_solo(run_root, iter_dir, spec, args.proposer_model,
                            timeout=args.lead_timeout)
        row: dict = {"iteration": it, "sessions": sessions,
                     "proposer_sec": round(time.time() - t0)}

        # Append-only gate: a session that edited history invalidates the iteration.
        tampered = [n for n, h in before.items()
                    if not (agents_dir / n).exists()
                    or hashlib.sha1((agents_dir / n).read_bytes()).hexdigest() != h]
        if tampered:
            for n in tampered:
                if (SEED_DIR / n).exists():
                    shutil.copy(SEED_DIR / n, agents_dir / n)
            row.update({"rejected": f"modified existing agents: {tampered}"})
            record(row)
            print(f"  REJECTED: modified existing agents {tampered}")
            continue

        if not pending.exists():
            row.update({"rejected": "no pending_eval.json"})
            record(row)
            print("  REJECTED: lead wrote no pending_eval.json")
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
        hits = static_scan(agent_file)
        if hits:
            row.update({"rejected": f"static scan: forbidden patterns {hits}"})
            record(row)
            print(f"  REJECTED: forbidden patterns {hits}")
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
