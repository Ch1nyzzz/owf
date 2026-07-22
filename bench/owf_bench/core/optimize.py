"""RSI driver: the outer optimization loop with mechanical health predicates.

Layer 0: workflows/_meta/optimizer.js edits the domain candidate each round.
Layer 1: workflows/_meta/watchdog.js is invoked ONLY when mechanical predicates
fire; it may repair/rewrite optimizer.js through the same validation gate, with
file-based versioning and last-known-good rollback.

Per round:
  1. prepare iter dir + evidence (frontier report, stability, notes persist at opt root)
  2. run optimizer.js (executor, domain=_meta, privileged tools)
  3. if a candidate landed: evaluate on train (k repeats), paired-compare vs frontier
     with the noise band; accept -> candidate becomes frontier
  4. compute health predicates; if fired -> run watchdog; apply verdict
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = ROOT / "executor"

STAGNATION_ROUNDS = 5
NO_CANDIDATE_STREAK = 3
WATCHDOG_COOLDOWN = 5  # rounds between watchdog invocations


def sh_executor(workflow: Path, task_file: Path, out_dir: Path, domain: str, max_tokens: int, max_sec: int) -> dict:
    proc = subprocess.run(
        ["npx", "tsx", "src/run.ts", "--workflow", str(workflow), "--task", str(task_file), "--out", str(out_dir),
         "--domain", domain, "--max-tokens", str(max_tokens), "--max-wallclock-sec", str(max_sec)],
        cwd=EXECUTOR, capture_output=True, text=True, timeout=max_sec + 180,
    )
    if proc.returncode != 0:
        return {"status": "infra_error", "error": proc.stderr[-800:]}
    return json.loads((out_dir / "result.json").read_text())


def validate_workflow(path: Path) -> tuple[bool, str]:
    proc = subprocess.run(["npx", "tsx", "src/run.ts", "--validate-only", str(path)],
                          cwd=EXECUTOR, capture_output=True, text=True, timeout=120)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def evaluate(workflow: Path, domain: str, out_dir: Path, limit: int | None, repeats: int, workers: int) -> dict | None:
    cmd = ["python3", str(ROOT / "bench/owf_bench/core/runner.py"), "--domain", domain, "--workflow", str(workflow),
           "--subset", "train", "--repeats", str(repeats), "--workers", str(workers), "--out", str(out_dir),
           "--max-tokens", "300000", "--max-wallclock-sec", "1800"]
    if limit:
        cmd += ["--limit", str(limit)]
    env = {"PYTHONPATH": str(ROOT / "bench")}
    import os
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=14400, env={**os.environ, **env})
    report_path = out_dir / "report.json"
    if not report_path.exists():
        print(f"  eval failed: {proc.stderr[-400:]}")
        return None
    return json.loads(report_path.read_text())


def opt_task_payload(opt_root: Path, domain: str, it: int, state: dict, opt_model: str) -> dict:
    frontier = state["frontier"]
    stability = opt_root / "evidence/stability.json"
    return {
        "id": f"opt-{domain}-iter{it:03d}",
        "instruction": (
            f"Optimization round {it} for domain '{domain}'.\n"
            f"Optimization root: {opt_root} (your notes: {opt_root}/NOTES.md; stability report: {stability}; "
            f"round history: {opt_root}/state.json; per-round artifacts under {opt_root}/iter_*/).\n"
            f"Current frontier workflow: {frontier['workflow']} — train score {frontier['score']} "
            f"(noise band ±{state.get('noise_band', 0.04)}), tokens/task {frontier.get('tokens_per_task')}.\n"
            f"Frontier eval report (per-task scores): {frontier.get('report')}\n"
            f"Rollout journals for any evaluated run are next to its report (task dirs with journal.jsonl and node transcripts).\n"
            f"Study the evidence, then write an improved candidate via write_workflow, update your notes, and submit your summary."
        ),
        "domain": domain,
        "opt_root": str(opt_root),
        "workflows_dir": str(ROOT / "workflows"),
        "candidate_path": str(opt_root / f"iter_{it:03d}/candidate.js"),
        "bench_root": str(ROOT / "bench"),
        "opt_model": opt_model,
    }


def compute_predicates(state: dict) -> list[str]:
    hist = state["history"]
    fired = []
    recent = hist[-NO_CANDIDATE_STREAK:]
    if len(recent) == NO_CANDIDATE_STREAK and all(not h["candidate_made"] for h in recent):
        fired.append(f"no_candidate_{NO_CANDIDATE_STREAK}_rounds")
    if hist and hist[-1].get("optimizer_status") in ("budget_exceeded", "timeout", "infra_error"):
        fired.append(f"optimizer_{hist[-1]['optimizer_status']}")
    band = state.get("noise_band", 0.04)
    scores = [h["frontier_score_after"] for h in hist]
    if len(scores) >= STAGNATION_ROUNDS and max(scores[-STAGNATION_ROUNDS:]) - scores[-STAGNATION_ROUNDS] <= band:
        fired.append(f"stagnation_{STAGNATION_ROUNDS}_rounds_within_noise_band")
    return fired


def run_watchdog(opt_root: Path, domain: str, it: int, predicates: list[str], opt_model: str, optimizer_path: Path, state: dict) -> None:
    wd_dir = opt_root / f"iter_{it:03d}/watchdog"
    wd_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "id": f"watchdog-{domain}-iter{it:03d}",
        "instruction": (
            f"Health predicates fired: {predicates}.\n"
            f"Optimizer source: {optimizer_path}\nOptimizer notes: {opt_root}/NOTES.md\n"
            f"Round history: {opt_root}/state.json\nPer-round optimizer journals: {opt_root}/iter_*/opt/journal.jsonl\n"
            f"Diagnose whether the optimizer is on the right rail; deliver your verdict."
        ),
        "domain": domain,
        "opt_root": str(opt_root),
        "workflows_dir": str(ROOT / "workflows"),
        "candidate_path": str(wd_dir / "unused.js"),
        "bench_root": str(ROOT / "bench"),
        "opt_model": opt_model,
    }
    task_file = wd_dir / "task.json"
    task_file.write_text(json.dumps(task))
    summary = sh_executor(ROOT / "workflows/_meta/watchdog.js", task_file, wd_dir, "_meta", 1_500_000, 3600)
    verdict = summary.get("result") if isinstance(summary.get("result"), dict) else {}
    event = {"iter": it, "predicates": predicates, "verdict": verdict.get("verdict"), "evidence": str(verdict.get("evidence"))[:2000]}

    if verdict.get("verdict") in ("process_pathology", "operational_fault") and verdict.get("rewrite"):
        staged = wd_dir / "optimizer.rewrite.js"
        staged.write_text(verdict["rewrite"])
        ok, msg = validate_workflow(staged)
        if ok:
            version = len([e for e in state["watchdog_events"] if e.get("applied")]) + 1
            backup = optimizer_path.with_suffix(f".js.v{version}.bak")
            shutil.copy(optimizer_path, backup)
            shutil.copy(staged, optimizer_path)
            event.update({"applied": True, "backup": str(backup)})
            print(f"  watchdog applied rewrite (backup: {backup.name})")
        else:
            event.update({"applied": False, "validation_error": msg})
            print(f"  watchdog rewrite REJECTED by validation gate: {msg}")
    state["watchdog_events"].append(event)
    state["last_watchdog_iter"] = it


def maybe_rollback(optimizer_path: Path, state: dict) -> None:
    """If the round right after an applied rewrite is an operational failure, restore the backup."""
    events = [e for e in state["watchdog_events"] if e.get("applied") and not e.get("rolled_back")]
    if not events or not state["history"]:
        return
    last_event, last_round = events[-1], state["history"][-1]
    if last_round["iter"] == last_event["iter"] + 1 and last_round.get("optimizer_status") in ("infra_error", "budget_exceeded", "timeout", "workflow_error"):
        shutil.copy(Path(last_event["backup"]), optimizer_path)
        last_event["rolled_back"] = True
        print(f"  ROLLBACK: restored {last_event['backup']} after post-rewrite failure")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True)
    p.add_argument("--opt-root", required=True)
    p.add_argument("--iters", type=int, default=10)
    p.add_argument("--seed-workflow", required=True, help="starting frontier workflow")
    p.add_argument("--baseline-run", required=True, help="k>=3 baseline run dir (report.json + stability.json)")
    p.add_argument("--eval-limit", type=int, help="tasks per candidate eval (default: full train)")
    p.add_argument("--eval-repeats", type=int, default=3)
    p.add_argument("--eval-workers", type=int, default=32)
    p.add_argument("--opt-model", default="kimi-k2")
    p.add_argument("--opt-max-tokens", type=int, default=2_000_000)
    p.add_argument("--opt-max-sec", type=int, default=5400)
    args = p.parse_args()

    opt_root = Path(args.opt_root).resolve()
    (opt_root / "evidence").mkdir(parents=True, exist_ok=True)
    optimizer_path = ROOT / "workflows/_meta/optimizer.js"

    baseline = Path(args.baseline_run).resolve()
    stability_src = baseline / "stability.json"
    if not stability_src.exists():
        raise SystemExit(f"stability.json missing in {baseline} — run stability.py first (k>=3 gate)")
    shutil.copy(stability_src, opt_root / "evidence/stability.json")
    stability = json.loads(stability_src.read_text())
    base_report = json.loads((baseline / "report.json").read_text())

    state_path = opt_root / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {
            "domain": args.domain,
            "noise_band": max(stability["noise_band"], 0.02),
            "frontier": {
                "workflow": str(Path(args.seed_workflow).resolve()),
                "score": base_report["score"],
                "tokens_per_task": base_report["tokens_per_task"],
                "report": str(baseline / "report.json"),
            },
            "history": [],
            "watchdog_events": [],
            "last_watchdog_iter": -10,
        }
    if not (opt_root / "NOTES.md").exists():
        (opt_root / "NOTES.md").write_text("(empty — first round)\n")

    start_iter = (state["history"][-1]["iter"] + 1) if state["history"] else 1
    for it in range(start_iter, start_iter + args.iters):
        print(f"=== iter {it} (frontier {state['frontier']['score']}) ===")
        iter_dir = opt_root / f"iter_{it:03d}"
        (iter_dir / "opt").mkdir(parents=True, exist_ok=True)

        task = opt_task_payload(opt_root, args.domain, it, state, args.opt_model)
        task_file = iter_dir / "opt_task.json"
        task_file.write_text(json.dumps(task))
        t0 = time.time()
        summary = sh_executor(optimizer_path, task_file, iter_dir / "opt", "_meta", args.opt_max_tokens, args.opt_max_sec)
        candidate = Path(task["candidate_path"])
        made = candidate.exists()
        rec = {
            "iter": it,
            "optimizer_status": summary.get("status", "unknown"),
            "optimizer_tokens": summary.get("totalTokens"),
            "optimizer_sec": round(time.time() - t0),
            "candidate_made": made,
            "result": summary.get("result"),
        }

        if made:
            # two-stage eval: k=1 screen (cheap), k=eval_repeats confirm only if promising.
            # Screening can miss a good candidate that gets unlucky at k=1 — accepted tradeoff;
            # the same run dir is reused so the confirm resumes r0 instead of re-running it.
            screen = evaluate(candidate, args.domain, iter_dir / "eval", args.eval_limit, 1, args.eval_workers)
            if screen:
                rec["screen_score"] = screen["score"]
                promising = screen["score"] > state["frontier"]["score"]
                report = None
                if promising and args.eval_repeats > 1:
                    report = evaluate(candidate, args.domain, iter_dir / "eval", args.eval_limit, args.eval_repeats, args.eval_workers)
                final = report or screen
                delta = final["score"] - state["frontier"]["score"]
                confirmed = report is not None or args.eval_repeats == 1
                accepted = confirmed and delta > state["noise_band"]
                rec.update({"candidate_score": final["score"], "delta": round(delta, 4), "accepted": accepted,
                            "confirmed_k": args.eval_repeats if report else 1,
                            "candidate_tokens_per_task": final["tokens_per_task"]})
                if accepted:
                    state["frontier"] = {"workflow": str(candidate), "score": final["score"],
                                         "tokens_per_task": final["tokens_per_task"], "report": str(iter_dir / "eval/report.json")}
                print(f"  candidate screen {screen['score']:.3f}" + (f" confirm {final['score']:.3f}" if report else "") + f" (delta {delta:+.3f}) -> {'ACCEPTED' if accepted else 'rejected'}")
            else:
                rec.update({"candidate_score": None, "accepted": False, "eval_failed": True})
        rec["frontier_score_after"] = state["frontier"]["score"]
        state["history"].append(rec)
        state_path.write_text(json.dumps(state, indent=1))

        maybe_rollback(optimizer_path, state)
        predicates = compute_predicates(state)
        if predicates and it - state["last_watchdog_iter"] >= WATCHDOG_COOLDOWN:
            print(f"  predicates fired: {predicates} -> watchdog")
            run_watchdog(opt_root, args.domain, it, predicates, args.opt_model, optimizer_path, state)
            state_path.write_text(json.dumps(state, indent=1))

    print(json.dumps({"frontier": state["frontier"], "rounds": len(state["history"]),
                      "watchdog_events": state["watchdog_events"]}, indent=1))


if __name__ == "__main__":
    main()
