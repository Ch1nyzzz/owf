"""RSI driver: the outer optimization loop with mechanical health predicates.

Layer 0: workflows/_meta/optimizer.js edits the domain candidate each round.
Layer 1: workflows/_meta/watchdog.js is invoked ONLY when mechanical predicates
fire; it may repair/rewrite optimizer.js through the same validation gate, with
file-based versioning and last-known-good rollback.

Per round:
  1. prepare iter dir + evidence (frontier report, stability, notes persist at opt root)
  2. run optimizer.js (executor, domain=_meta, privileged tools)
  3. if a candidate landed: evaluate on train (k repeats), then place it on the
     (score up, tokens down) Pareto frontier — it enters unless an existing point
     dominates it, and evicts the points it dominates
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

# After this many rounds without a frontier entry, the round's instruction adds a reason
# to re-open design-level decisions (topology, decomposition, roles). Runs freeze their
# structure early — every observed run settles its topology within its first few rounds
# and spends the rest on prompt-level edits — and a stalled frontier is the evidence that
# the current design's neighborhood has stopped paying. Fires before the watchdog
# (STAGNATION_ROUNDS=5): a stalled optimizer gets the evidence first, the watchdog only
# if stalling continues.
EXPLORE_HINT_ROUNDS = 3

# Frontier admission is exact: a higher train score is a higher train score.
#
# This was 0.0618 (realmath's measured noise band) and it cost us a real point. Round 4's
# 0.640 @ 131,842 and round 2's 0.680 @ 146,698 differ by 0.040 — inside the band, so the
# driver called them tied, compared tokens, and evicted the higher-scoring point. That is
# the band deciding a statistical question the frontier has no standing to decide: with
# k=1 evidence it cannot tell a 0.04 lucky draw from a 0.04 real gain, and guessing "noise"
# throws away a candidate no later round can recover.
#
# Setting it to 0 only ever makes domination HARDER (a point must be no worse on score to
# evict another), so the set grows rather than churns — the failure mode is a longer
# frontier table, not a lost candidate. The statistical load moves to where the evidence
# can carry it: the champion still owes k=3 confirmation plus the held-out test set.
# The measured band stays in state.json and in the optimizer's prompt as EVIDENCE about
# how much its scores wobble; it just no longer overrides the measurement.
SCORE_BAND = 0.0


def dominates(a: dict, b: dict, band: float = 0.0) -> bool:
    """Does `a` dominate `b` on (score up, tokens down)?

    `band` is a score tolerance: differences within it count as ties rather than wins.
    The driver passes SCORE_BAND (0.0) — see the note there for why. The parameter
    survives because it is the honest way to express the comparison and the unit tests
    pin both behaviours; nothing in the loop supplies a non-zero value.
    """
    score_better = a["score"] - b["score"] > band
    score_worse = b["score"] - a["score"] > band
    tokens_better = a["tokens"] < b["tokens"]
    tokens_worse = a["tokens"] > b["tokens"]
    if score_worse or tokens_worse:
        return False
    return score_better or tokens_better


def update_frontier(frontier: list[dict], candidate: dict, band: float) -> tuple[list[dict], bool]:
    """Insert `candidate` into the non-dominated set. Returns (new frontier, entered?)."""
    if any(dominates(point, candidate, band) for point in frontier):
        return frontier, False
    kept = [point for point in frontier if not dominates(candidate, point, band)]
    return sorted([*kept, candidate], key=lambda p: (-p["score"], p["tokens"])), True


def best_by_score(frontier: list[dict]) -> dict:
    """Highest score, leanest among ties — the point reported as the run's champion."""
    return min(frontier, key=lambda p: (-p["score"], p["tokens"]))


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


def evaluate(workflow: Path, domain: str, out_dir: Path, limit: int | None, repeats: int, workers: int,
             max_tokens: int, max_sec: int) -> dict | None:
    # The per-rollout budget MUST match the one the baseline was measured under, or the
    # candidate is compared against a frontier it was never allowed to reach (realmath's
    # baseline ran at 300k, bcplus's at 600k — a shared hardcoded cap silently penalises one).
    cmd = ["python3", str(ROOT / "bench/owf_bench/core/runner.py"), "--domain", domain, "--workflow", str(workflow),
           "--subset", "train", "--repeats", str(repeats), "--workers", str(workers), "--out", str(out_dir),
           "--max-tokens", str(max_tokens), "--max-wallclock-sec", str(max_sec)]
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


def frontier_table(frontier: list[dict], band: float) -> str:
    """The Pareto set as the optimizer sees it — one line per non-dominated point."""
    lines = []
    for i, p in enumerate(frontier, 1):
        tok = p.get("tokens_per_task") or {}
        lines.append(
            f"  [{i}] score {p['score']:.4f} | {p['tokens']:,} tokens/task "
            f"({tok.get('input', '?')} in + {tok.get('output', '?')} out) | {p['workflow']}\n"
            f"      per-task scores: {p.get('report')}"
        )
    return "\n".join(lines)


def stalled_rounds(history: list[dict]) -> int:
    """Consecutive trailing rounds without a frontier entry (a no-candidate round counts)."""
    n = 0
    for h in reversed(history):
        if h.get("entered_frontier"):
            break
        n += 1
    return n


def write_train_gold(opt_root: Path, domain: str, data_root: Path = ROOT / "data") -> None:
    """Expose TRAIN-split gold answers as optimizer evidence (evidence/train_gold.json).

    The information boundary is the held-out test split, not gold per se: the candidate
    workflow never sees gold (runner.py strips it), but the optimizer may see everything
    on the train side. Its read scope stops at opt_root, so this file is the sanctioned
    channel — without it a reader scanning a failed rollout has no way to decide whether
    anything the trajectory produced was actually correct. Train ids only; test stays sealed.
    """
    tasks_file = data_root / domain / "tasks.jsonl"
    split_file = data_root / domain / "split.json"
    if not tasks_file.exists() or not split_file.exists():
        return  # bridged domains (tb2/harbor) keep their data elsewhere; nothing to expose
    train_ids = set(json.loads(split_file.read_text())["train"])
    gold = {}
    for line in tasks_file.read_text().splitlines():
        if not line.strip():
            continue
        task = json.loads(line)
        if task["id"] in train_ids and "gold" in task:
            gold[task["id"]] = task["gold"]
    (opt_root / "evidence").mkdir(parents=True, exist_ok=True)  # callers outside optimize.py (meta-harness arm) have no evidence dir yet
    (opt_root / "evidence/train_gold.json").write_text(json.dumps(gold, ensure_ascii=False, indent=1))


def opt_task_payload(opt_root: Path, domain: str, it: int, state: dict, opt_model: str, opt_thinking: str,
                     eval_max_tokens: int, eval_max_sec: int) -> dict:
    frontier = state["frontier"]
    band = state.get("noise_band", 0.04)
    stability = opt_root / "evidence/stability.json"
    train_gold = opt_root / "evidence/train_gold.json"
    gold_line = (
        f"Train-set gold answers: {train_gold} (task_id -> gold) — evidence for judging what a rollout "
        f"actually produced against what was required.\n"
    ) if train_gold.exists() else ""
    stalled = stalled_rounds(state["history"])
    stalled_line = (
        f"The frontier has not moved in the last {stalled} rounds. That is evidence about the design "
        f"neighborhood, not only about the individual edits: refinements of the incumbent shape have "
        f"stopped clearing the bar, so weigh whether the next gain lives at the design level — "
        f"decomposition, topology, node roles, handoffs, budget split — and what the failure evidence "
        f"says such a redesign should look like. Every earlier candidate sits under iter_*/ with its "
        f"eval report, so designs already tried are a read away.\n"
    ) if stalled >= EXPLORE_HINT_ROUNDS else ""
    return {
        "id": f"opt-{domain}-iter{it:03d}",
        "instruction": (
            f"Optimization round {it} for domain '{domain}'.\n"
            f"Optimization root: {opt_root} (your notes: {opt_root}/NOTES.md; stability report: {stability}; "
            f"round history: {opt_root}/state.json; per-round artifacts under {opt_root}/iter_*/).\n"
            f"\nCURRENT PARETO FRONTIER — {len(frontier)} non-dominated point(s) on (score up, tokens down):\n"
            f"{frontier_table(frontier, band)}\n"
            f"A candidate enters the frontier by not being dominated: it must beat some point on score "
            f"or use fewer tokens, without being worse on the other axis. Admission is exact — a higher "
            f"train score is a higher train score, and no noise tolerance is applied. For context on how "
            f"much these scores wobble, the baseline measured a ±{band} run-to-run band across k repeats "
            f"(evidence/stability.json); weigh that when you judge whether a small delta is worth building "
            f"on, but the frontier records what was measured, not what survived a significance test. "
            f"Tokens are input+output per task, counting cache-miss input only. Every node, every turn and "
            f"every word a node is asked to write spends this budget.\n"
            f"Parent choice is YOURS: any frontier point, any earlier candidate (iter_*/candidate.js, each with "
            f"its eval report), or a graft across them — rejected candidates often contain good ideas that did "
            f"not clear the bar alone. State your chosen parent(s) in your notes.\n"
            f"Rollout journals for any evaluated run sit next to its report: one dir per (task, repeat), "
            f"each with journal.jsonl and per-node transcripts. The baseline run — every rollout behind the "
            f"stability report — is linked at {opt_root}/evidence/baseline.\n"
            f"{gold_line}"
            f"{stalled_line}"
            f"Your read scope is exactly {opt_root} and {ROOT / 'workflows'}; all evidence lives inside it. "
            f"Paths outside are refused by design (the held-out test split stays sealed), so do not spend turns probing them.\n"
            f"Study the evidence, then write an improved candidate via write_workflow, update your notes, and submit your summary."
        ),
        "domain": domain,
        "opt_root": str(opt_root),
        "workflows_dir": str(ROOT / "workflows"),
        "candidate_path": str(opt_root / f"iter_{it:03d}/candidate.js"),
        "bench_root": str(ROOT / "bench"),
        "opt_model": opt_model,
        "opt_thinking": opt_thinking,
        # run_probe must evaluate under the same per-rollout budget as the real eval.
        "eval_max_tokens": eval_max_tokens,
        "eval_max_sec": eval_max_sec,
    }


def compute_predicates(state: dict) -> list[str]:
    hist = state["history"]
    fired = []
    recent = hist[-NO_CANDIDATE_STREAK:]
    if len(recent) == NO_CANDIDATE_STREAK and all(not h["candidate_made"] for h in recent):
        fired.append(f"no_candidate_{NO_CANDIDATE_STREAK}_rounds")
    last = hist[-1] if hist else None
    if last and last.get("optimizer_status") in ("budget_exceeded", "timeout", "infra_error"):
        fired.append(f"optimizer_{last['optimizer_status']}")
    # A dead lead node does not show up in optimizer_status. optimizer.js catches the node's
    # null and returns a fallback object, so the workflow exits cleanly and run.ts records
    # "ok" — bcplus v2 round 8 read as a healthy round while its lead had died on a transport
    # error. Only the fallback knows, so it says so via node_failed and we read it here.
    # The round is not necessarily lost: write_workflow lands the candidate the moment it is
    # called, so a lead that died after writing still leaves one behind and it is still
    # evaluated. What fires here is the watchdog, not a retry.
    if last and isinstance(last.get("result"), dict) and last["result"].get("node_failed"):
        fired.append("optimizer_lead_node_failed")
    # Stagnation is now "the frontier stopped moving", not "the top score stopped rising":
    # a round that only made things cheaper is real progress on the second axis.
    recent_rounds = hist[-STAGNATION_ROUNDS:]
    if len(recent_rounds) == STAGNATION_ROUNDS and not any(h.get("entered_frontier") for h in recent_rounds):
        fired.append(f"stagnation_{STAGNATION_ROUNDS}_rounds_no_frontier_entry")
    return fired


def run_watchdog(opt_root: Path, domain: str, it: int, predicates: list[str], opt_model: str, optimizer_path: Path, state: dict) -> None:
    """Diagnose the optimizer; apply a rewrite if the watchdog staged one.

    The rewrite arrives as a FILE (write_workflow -> optimizer.rewrite.js), not as a field
    of the final verdict. It used to be a schema string, and bcplus round 4 showed why that
    loses work: the watchdog finished its diagnosis and emitted a correct 15KB rewrite, but
    the response carrying it died mid-stream (stopReason=error, usage all zero), so the node
    returned null and the driver recorded "watchdog node failed; defaulting to no-op". A
    17KB single-shot submission is the whole round's output riding on one response.

    Writing through a tool fixes the two things that mattered: the source lands on disk the
    moment it is written, so a later failure cannot unwrite it, and the validation gate
    answers inline, so a rejected rewrite can be fixed and retried instead of killing the
    round. The verdict itself is then small enough to be cheap to deliver.
    """
    wd_dir = opt_root / f"iter_{it:03d}/watchdog"
    wd_dir.mkdir(parents=True, exist_ok=True)
    staged = wd_dir / "optimizer.rewrite.js"
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
        # write_workflow's fixed destination. For the watchdog that IS the deliverable.
        "candidate_path": str(staged),
        "bench_root": str(ROOT / "bench"),
        "opt_model": opt_model,
        "opt_thinking": "xhigh",
    }
    task_file = wd_dir / "task.json"
    task_file.write_text(json.dumps(task))
    # Budget covers the watchdog's reader subagents: diagnosing a stagnation predicate means
    # sweeping several rounds of optimizer history, and one optimizer round alone is ~400KB.
    summary = sh_executor(ROOT / "workflows/_meta/watchdog.js", task_file, wd_dir, "_meta", 4_000_000, 5400)
    verdict = summary.get("result") if isinstance(summary.get("result"), dict) else {}
    event = {"iter": it, "predicates": predicates, "verdict": verdict.get("verdict"), "evidence": str(verdict.get("evidence"))[:2000]}

    # Backward compatibility: a watchdog that still returns the source inline gets it staged
    # for it, so an older workflows/_meta/watchdog.js keeps working against this driver.
    if not staged.exists() and verdict.get("rewrite"):
        staged.write_text(verdict["rewrite"])

    # A staged file means the watchdog decided to intervene, whether or not its verdict
    # survived the trip back. Trusting the file over the verdict is what makes the channel
    # crash-tolerant; a rewrite that lands and then regresses is caught by maybe_rollback.
    if staged.exists() and verdict.get("verdict") != "healthy_stagnation":
        if not verdict.get("verdict"):
            event["verdict_missing"] = True
            print("  watchdog staged a rewrite but returned no verdict — applying the file it wrote")
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
    # Protocol: k=3 is spent exactly twice — pre-run stability measurement (source of the
    # noise band) and final champion confirmation. Inside the loop k=1 + the noise-band
    # acceptance threshold carries the statistical load.
    p.add_argument("--eval-repeats", type=int, default=1)
    p.add_argument("--eval-workers", type=int, default=32)
    # Must equal the baseline run's cap; see evaluate(). realmath: 300000, bcplus: 600000.
    p.add_argument("--eval-max-tokens", type=int, default=300_000)
    p.add_argument("--eval-max-sec", type=int, default=1800)
    p.add_argument("--opt-model", default="gpt-5.6-terra")
    p.add_argument("--opt-thinking", default="xhigh")
    p.add_argument("--opt-max-tokens", type=int, default=2_000_000)
    p.add_argument("--opt-max-sec", type=int, default=5400)
    args = p.parse_args()

    opt_root = Path(args.opt_root).resolve()
    (opt_root / "evidence").mkdir(parents=True, exist_ok=True)

    # Each run owns a private copy of the optimizer. The watchdog rewrites this file in
    # place, so concurrent runs sharing workflows/_meta/optimizer.js would silently swap
    # each other's optimizer mid-experiment — §五 wants a watchdog intervention to be a
    # versioned event within ONE arm, not a global one. The copy also keeps the source
    # inside the optimizer's own read scope, so it can inspect itself.
    optimizer_path = opt_root / "optimizer.js"
    if not optimizer_path.exists():
        shutil.copy(ROOT / "workflows/_meta/optimizer.js", optimizer_path)

    baseline = Path(args.baseline_run).resolve()
    stability_src = baseline / "stability.json"
    if not stability_src.exists():
        raise SystemExit(f"stability.json missing in {baseline} — run stability.py first (k>=3 gate)")
    shutil.copy(stability_src, opt_root / "evidence/stability.json")
    shutil.copy(ROOT / "docs/DSL.md", opt_root / "evidence/DSL.md")  # the action-space reference
    write_train_gold(opt_root, args.domain)  # regenerated each launch so a split change propagates

    # The optimizer's read scope is opt_root + workflows/ (executor/src/tools/meta.ts).
    # The baseline run is a sibling directory outside that scope, so link it in — without
    # this the optimizer can reach neither the per-task frontier report nor a single
    # baseline rollout journal, and round 1 has to guess from aggregates alone.
    # A symlink suffices: meta.ts scopes with path.resolve, which does not follow links.
    baseline_link = opt_root / "evidence/baseline"
    if not baseline_link.exists():
        baseline_link.symlink_to(baseline)

    stability = json.loads(stability_src.read_text())
    base_report = json.loads((baseline / "report.json").read_text())
    base_tokens = base_report.get("tokens_per_task_total")
    if base_tokens is None:  # reports predating the field still carry the split
        t = base_report["tokens_per_task"]
        base_tokens = t["input"] + t["output"]

    state_path = opt_root / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {
            "domain": args.domain,
            # Recorded and shown to the optimizer as evidence about score stability.
            # It does NOT gate frontier admission any more — see SCORE_BAND.
            "noise_band": stability["noise_band"],
            # Pareto set on (score up, tokens down); the baseline is its first point.
            "frontier": [
                {
                    "workflow": str(Path(args.seed_workflow).resolve()),
                    "score": base_report["score"],
                    "tokens": base_tokens,
                    "tokens_per_task": base_report["tokens_per_task"],
                    "report": str(baseline_link / "report.json"),  # in-scope path, not the raw sibling dir
                }
            ],
            "history": [],
            "watchdog_events": [],
            "last_watchdog_iter": -10,
        }
    if isinstance(state.get("frontier"), dict):
        raise SystemExit(
            f"{state_path} holds a single-point frontier from the pre-Pareto driver, which recorded only "
            "the winner of each round — the dominated points it discarded cannot be recovered. Start a "
            "fresh --opt-root."
        )
    if not (opt_root / "NOTES.md").exists():
        (opt_root / "NOTES.md").write_text("(empty — first round)\n")

    start_iter = (state["history"][-1]["iter"] + 1) if state["history"] else 1
    for it in range(start_iter, start_iter + args.iters):
        top = best_by_score(state["frontier"])
        print(f"=== iter {it} (frontier: {len(state['frontier'])} pts, best {top['score']:.3f} @ {top['tokens']:,} tok) ===")
        iter_dir = opt_root / f"iter_{it:03d}"
        (iter_dir / "opt").mkdir(parents=True, exist_ok=True)

        task = opt_task_payload(opt_root, args.domain, it, state, args.opt_model, args.opt_thinking,
                                args.eval_max_tokens, args.eval_max_sec)
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
            # No mid-loop certification gate: every candidate's measured score is just data,
            # and the optimizer still self-selects parents from the full history. The frontier
            # is bookkeeping — the non-dominated set so far, served to the next round as the
            # default set of parents. Luck inflation is settled at the END: the champion must
            # pass k=3 confirmation + the held-out test set.
            report = evaluate(candidate, args.domain, iter_dir / "eval", args.eval_limit, args.eval_repeats,
                              args.eval_workers, args.eval_max_tokens, args.eval_max_sec)
            if report:
                cand_tokens = report["tokens_per_task"]["input"] + report["tokens_per_task"]["output"]
                point = {"workflow": str(candidate), "score": report["score"], "tokens": cand_tokens,
                         "tokens_per_task": report["tokens_per_task"], "report": str(iter_dir / "eval/report.json")}
                before = best_by_score(state["frontier"])
                state["frontier"], entered = update_frontier(state["frontier"], point, SCORE_BAND)
                rec.update({"candidate_score": report["score"], "candidate_tokens": cand_tokens,
                            "delta": round(report["score"] - before["score"], 4),
                            "entered_frontier": entered,
                            "candidate_tokens_per_task": report["tokens_per_task"]})
                verdict = f"-> frontier ({len(state['frontier'])} pts)" if entered else "dominated"
                print(f"  candidate {report['score']:.3f} @ {cand_tokens:,} tok {verdict}")
            else:
                rec.update({"candidate_score": None, "entered_frontier": False, "eval_failed": True})
        best = best_by_score(state["frontier"])
        rec["frontier_after"] = {"points": len(state["frontier"]), "best_score": best["score"], "best_tokens": best["tokens"]}
        state["history"].append(rec)
        state_path.write_text(json.dumps(state, indent=1))

        maybe_rollback(optimizer_path, state)
        predicates = compute_predicates(state)
        if predicates and it - state["last_watchdog_iter"] >= WATCHDOG_COOLDOWN:
            print(f"  predicates fired: {predicates} -> watchdog")
            run_watchdog(opt_root, args.domain, it, predicates, args.opt_model, optimizer_path, state)
            state_path.write_text(json.dumps(state, indent=1))

    # The champion still owes a k=3 confirmation and the held-out test set; the frontier
    # is measured at k=1 and picking its best point is exactly where luck accumulates.
    print(json.dumps({"frontier": state["frontier"], "champion_by_score": best_by_score(state["frontier"]),
                      "rounds": len(state["history"]), "watchdog_events": state["watchdog_events"]}, indent=1))


if __name__ == "__main__":
    main()
