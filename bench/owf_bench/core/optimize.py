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
  4. update the per-task champion book (task_book.py): coverage is the search
     objective — a candidate that claims unsolved tasks or takes owned tasks
     cheaper is progress even when the aggregate frontier does not move
  5. compute health predicates; if fired -> run watchdog; apply verdict
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from owf_bench.core import task_book

ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = ROOT / "executor"

STAGNATION_ROUNDS = 5
NO_CANDIDATE_STREAK = 3
WATCHDOG_COOLDOWN = 5  # rounds between watchdog invocations

# After this many rounds without a frontier entry, the round's instruction adds a reason
# to re-open design-level decisions (topology, decomposition, roles). LEGACY: only the
# workflow-proposer path (opt_task_payload) still injects this hint; the codex path and
# the meta-harness arm run unsteered for arm parity — their prompts differ only by the
# orientation block.
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


def build_gold_line(opt_root: Path) -> str:
    train_gold = opt_root / "evidence/train_gold.json"
    return (
        f"Train-set gold answers: {train_gold} (task_id -> gold) — evidence for judging what a rollout "
        f"actually produced against what was required.\n"
    ) if train_gold.exists() else ""


def build_coverage_block(opt_root: Path) -> str:
    """The lab-coverage view of the task book, as evidence for the search round.

    Summary only — per-task detail stays in task_book.json, which is inside the
    optimizer's read scope; the block names the path so the round can drill in.
    """
    book_path = opt_root / "task_book.json"
    if not book_path.exists():
        return ""
    book = json.loads(book_path.read_text())
    cov = book["coverage"]
    cover = " + ".join(f"{c['member']}(+{c['marginal']})" for c in book["cover_set"]) or "none"
    return (
        f"\nLAB COVERAGE (task book: {book_path}) — the search objective is coverage of the task set by "
        f"the roster as a whole, not any single member's average score:\n"
        f"  solved at least once: {cov['union']}/{cov['universe']} | reproducibly confirmed "
        f"(>= {book['params']['confirm_reps']} reps): {cov['confirmed']}/{cov['universe']} | "
        f"greedy cover set: {cover}\n"
        f"  UNSOLVED BY EVERY MEMBER ({len(cov['unsolved'])}): {', '.join(cov['unsolved']) or 'none'}\n"
        f"  A candidate that solves ANY unsolved task, or takes owned tasks cheaper or more stably, is "
        f"real progress even if its average score enters no frontier. The task book records each task's "
        f"owner, contenders and passing-rollout pointers; the failed rollouts on unsolved tasks are the "
        f"residual evidence to mine.\n"
    )


def build_stalled_line(state: dict) -> str:
    stalled = stalled_rounds(state["history"])
    return (
        f"The frontier has not moved in the last {stalled} rounds — refinement has stopped paying; "
        f"weigh a redesign.\n"
    ) if stalled >= EXPLORE_HINT_ROUNDS else ""


def opt_task_payload(opt_root: Path, domain: str, it: int, state: dict, opt_model: str, opt_thinking: str,
                     eval_max_tokens: int, eval_max_sec: int) -> dict:
    frontier = state["frontier"]
    band = state.get("noise_band", 0.04)
    stability = opt_root / "evidence/stability.json"
    gold_line = build_gold_line(opt_root)
    stalled_line = build_stalled_line(state)
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
            f"{build_coverage_block(opt_root)}"
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


WORKFLOW_REP_CONTRACT = """THE REPRESENTATION — a workflow.js orchestration program (full reference: evidence/DSL.md):
- Module shape: `export const meta = { name }` + `export default async function run(ctx)`.
- ctx.agent(prompt, {system, model, tools, maxTurns, temperature, thinkingLevel, schema, label, hooks}) — one full agent rollout (a subagent). Every node writes its OWN system prompt. Returns final text / schema-validated object / null on failure.
- Orchestration: ctx.pipeline(items, ...stages) (per-item flow, no barrier), ctx.parallel(thunks) (barrier), plain JS glue, ctx.budget for spend introspection. Multi-node structures — decompose→route→assemble, parallel explorers + judge, verify loops — are all expressible.
- Model routing: deepseek-v4-flash is the ONLY model a candidate may select. Model choice is NOT a design axis — what varies is topology, per-node system prompts, turn budgets, tools, and rails. A "stronger reviewer" has to be built out of prompt, evidence, and structure, not a bigger model.
- In-loop rails via hooks on any node: preToolUse->{block} | postToolUse->{inject} | onTurn->{stop|inject} | onStop->{continue, max 5}. Hooks may call ctx.agent — an LLM as a rail.
- Structured output via the schema option; harness-fixed domain tools per DSL.md; custom tools via ctx.defineTool (handler may call ctx.agent or ctx.runTool).
- Hard boundary: no new side-effect channels (raw network/fs/process), no information sources outside the benchmark rules, no bypassing token accounting. The repo's data/ directory is OFF-LIMITS — held-out gold lives there; evidence/train_gold.json is the only sanctioned gold channel."""


SLOT_VOCAB_V1 = {
    "research_prompt": "system prompt of the investigator node",
    "decoding": "temperature / thinking settings",
    "turn_budget": "maxTurns of the investigator",
    "cutoff_rail": "hard research deadline (onTurn inject + preToolUse block)",
    "closure": "converts a deadline-hitting run into an answer",
    "output_contract": "how the answer leaves the workflow",
    "verifier": "optional post-hoc answer checks",
    "monitor_rail": "mid-run trajectory monitors",
    "topology": "node structure of the workflow",
}

MANIFEST_CONTRACT = """
DECLARATION PROTOCOL (contract-native search; docs/CONTRACTS.md). Anonymous landing is
forbidden: summary.json MUST also carry a "manifest" declaring what the candidate is made of:
  "manifest": {
    "action": "NEW_COMPONENT" | "NEW_ASSEMBLY" | "NEW_SLOT",
    "components": {"<slot>.<name>": {"slot": "<slot>", "desc": "<one line>", "params": {...}}, ...},
    "new_slots": {"<slot>": "<one line>"}        // only with action NEW_SLOT
  }
"components" lists EVERY component the candidate carries. Reuse an existing inventory id
verbatim for anything kept from a parent; declare a NEW id whenever prompt text or logic
changes (a param-only change reuses the id with different params). Actions:
  NEW_ASSEMBLY  — recombination / param change of EXISTING inventory components only.
  NEW_COMPONENT — introduces new component id(s) into existing slots.
  NEW_SLOT      — grows the slot vocabulary itself; ship the new slot(s) plus their first component(s).
Slot vocabulary: research_prompt, decoding, turn_budget, cutoff_rail, closure, output_contract,
verifier, monitor_rail, topology. The live inventory (ids, slots, who carries what) is at
<opt_root>/lab/components.json. An invalid or missing manifest voids the round."""

MANIFEST_ID_RE = r"^[a-z][a-z0-9_]*\.[a-z0-9_]+$"

PROBE_CONTRACT = """
PROBE (sanctioned pilot runs). Before finalizing, you MAY test your candidate on a few
specific train tasks:
  PYTHONPATH={root}/bench python3 {root}/bench/owf_bench/core/runner.py --domain {domain} \\
    --workflow {candidate} --subset train --task-ids <id1,id2,...> --repeats 1 --workers 4 \\
    --out {iter_dir}/probe/<name> --max-tokens {max_tokens} --max-wallclock-sec {max_sec}
Rules: at most 8 probe rollouts per round (they spend real SUT tokens); probe under the SAME
budgets as the real eval (flags above, do not change them); probe results (probe/<name>/
results.jsonl, scored) are YOUR private k=1 evidence for this round's decisions — they never
enter the task book. Spend probes where they decide something: the unsolved residual, or a
task your change specifically targets. Probing is optional; a well-evidenced round may skip it."""


def validate_manifest(manifest: object, inventory: dict) -> list[str]:
    import re
    if not isinstance(manifest, dict):
        return ["manifest missing or not an object"]
    errors = []
    action = manifest.get("action")
    if action not in ("NEW_COMPONENT", "NEW_ASSEMBLY", "NEW_SLOT"):
        errors.append(f"unknown action: {action}")
    comps = manifest.get("components")
    if not isinstance(comps, dict) or not comps:
        return errors + ["components must be a non-empty object"]
    new_slots = manifest.get("new_slots") or {}
    known_slots = set(inventory.get("slots", {})) | set(new_slots)
    known_ids = set(inventory.get("components", {}))
    fresh = [cid for cid in comps if cid not in known_ids]
    for cid, spec in comps.items():
        if not re.match(MANIFEST_ID_RE, cid):
            errors.append(f"bad component id: {cid}")
        if not isinstance(spec, dict) or spec.get("slot") not in known_slots:
            errors.append(f"{cid}: unknown or missing slot {spec.get('slot') if isinstance(spec, dict) else spec}")
        elif cid in fresh and not (spec.get("desc") or "").strip():
            errors.append(f"{cid}: new component needs a desc")
    if action == "NEW_ASSEMBLY" and fresh:
        errors.append(f"NEW_ASSEMBLY may not introduce components: {fresh}")
    if action == "NEW_COMPONENT" and not fresh:
        errors.append("NEW_COMPONENT declared but every component already exists")
    if action == "NEW_SLOT" and not new_slots:
        errors.append("NEW_SLOT requires new_slots")
    return errors


def apply_manifest(inventory: dict, member: str, manifest: dict) -> None:
    """Record a validated manifest: inventory rows grow, never rewrite history."""
    for slot, desc in (manifest.get("new_slots") or {}).items():
        inventory.setdefault("slots", {}).setdefault(slot, str(desc))
    params: dict = {}
    for cid, spec in manifest["components"].items():
        inventory.setdefault("components", {}).setdefault(cid, {"slot": spec["slot"], "desc": spec.get("desc", "")})
        if spec.get("params"):
            params[cid] = spec["params"]
    inventory.setdefault("members", {})[member] = {cid: 1 for cid in manifest["components"]}
    if params:
        inventory.setdefault("params", {})[member] = params
    inventory.setdefault("manifest_log", []).append({"member": member, "action": manifest["action"]})


def init_inventory(opt_root: Path) -> Path:
    """v9 starts from the seed alone: slot vocabulary + the seed's own manifest, no imports."""
    path = opt_root / "lab/components.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "slots": SLOT_VOCAB_V1,
            "components": {"prompt.seed_persistent": {
                "slot": "research_prompt",
                "desc": "seed's persistent-researcher prompt: vary keywords, reformulate, commit before turns run out"}},
            "members": {"seed": {"prompt.seed_persistent": 1}},
            "params": {"seed": {"prompt.seed_persistent": {"turn_budget": 64}}},
            "manifest_log": [{"member": "seed", "action": "SEED"}],
        }, indent=1))
    return path


def build_inventory_block(opt_root: Path) -> str:
    path = opt_root / "lab/components.json"
    if not path.exists():
        return ""
    inv = json.loads(path.read_text())
    by_slot: dict[str, list[str]] = {}
    for cid, spec in inv.get("components", {}).items():
        carriers = sum(1 for comps in inv.get("members", {}).values() if comps.get(cid))
        by_slot.setdefault(spec.get("slot", "?"), []).append(f"{cid} (x{carriers})")
    lines = [f"  {slot}: {', '.join(sorted(ids))}" for slot, ids in sorted(by_slot.items())]
    return (
        f"\nCOMPONENT INVENTORY ({path}) — ids by slot (x carriers). Reuse ids verbatim; "
        f"a NEW_ASSEMBLY of untested combinations is a legitimate, cheap round:\n" + "\n".join(lines) + "\n"
    )


def codex_evidence_text(opt_root: Path, domain: str, it: int, state: dict) -> str:
    # Deliberately unsteered (arm parity with meta_loop.py): no first-round redesign mandate,
    # no stalled-frontier hint, no refuted-hypothesis rule. The arms' prompts differ only by
    # the orientation block injected in run_codex_round.
    frontier = state["frontier"]
    band = state.get("noise_band", 0.04)
    band_line = (
        f"For context on how much scores wobble, the baseline measured a ±{band} run-to-run band "
        "across k repeats (evidence/stability.json). "
    ) if band else (
        "The baseline was measured once (k=1); run-to-run variance is UNMEASURED on this substrate — "
        "treat small score deltas as unresolved. "
    )
    return (
        f"Search round {it} for domain '{domain}' — the team-building phase: the deliverable of this run "
        f"is a roster of workflows that together cover the task set, not one champion.\n"
        f"Working directory: {opt_root} (the optimization root).\n\n"
        f"CURRENT PARETO FRONTIER — {len(frontier)} non-dominated point(s) on (score up, tokens down):\n"
        f"{frontier_table(frontier, band)}\n"
        "A candidate enters the frontier by not being dominated: it must beat some point on score or use "
        "fewer tokens, without being worse on the other axis. Admission is exact — no noise tolerance is "
        f"applied. {band_line}Tokens are input+output per task.\n"
        f"{build_coverage_block(opt_root)}"
        f"{build_inventory_block(opt_root)}"
        f"{build_gold_line(opt_root)}"
        "Evidence layout: evidence/stability.json (per-task stability); evidence/baseline/ — the canonical "
        "seed rollouts (one dir per task: journal.jsonl + full per-node transcripts); iter_*/eval/ — every "
        "prior candidate's evaluation (results.jsonl, report.json, one rollout dir per task); "
        "iter_*/candidate.js — every prior candidate's source; iter_*/optimizer.out.md — prior rounds' "
        "deliberation; state.json — round history; NOTES.md — cross-round memory."
    )


def run_codex_round(opt_root: Path, domain: str, it: int, state: dict, iter_dir: Path,
                    opt_model: str, lead_timeout: int, contract_native: bool = False,
                    eval_max_tokens: int = 600_000, eval_max_sec: int = 1800) -> tuple[Path, dict]:
    """One optimization round: a single codex session (protocol shared with the meta-harness arm).

    The only prompt-level difference from the baseline arm is ORIENTATION_SWARM — the
    standing preference for designing swarm collaboration schemes. That block, plus the
    workflow-DSL representation, is what this arm tests.
    """
    from owf_bench.core.codex_solo import ORIENTATION_SWARM, ArmSpec, run_solo

    candidate = iter_dir / "candidate.js"
    manifest_line = (
        f'- Also write {iter_dir}/summary.json: {{"made_candidate": bool, "summary": str, '
        f'"manifest": {{...per the DECLARATION PROTOCOL...}}, "notes": str (optional)}}.'
    ) if contract_native else (
        f'- Also write {iter_dir}/summary.json: {{"made_candidate": bool, "summary": str, '
        f'"notes": str (optional)}}.'
    )
    probe_block = PROBE_CONTRACT.format(root=ROOT, domain=domain, candidate=candidate,
                                        iter_dir=iter_dir, max_tokens=eval_max_tokens,
                                        max_sec=eval_max_sec) if contract_native else ""
    spec = ArmSpec(
        subject="an agent workflow (a workflow.js orchestration program)",
        rep_contract=WORKFLOW_REP_CONTRACT + (MANIFEST_CONTRACT if contract_native else "") + probe_block,
        evidence_text=codex_evidence_text(opt_root, domain, it, state),
        candidate_path=candidate,
        validate_cmd=f"cd {EXECUTOR} && npx tsx src/run.ts --validate-only {candidate}",
        extra=manifest_line,
        orientation=ORIENTATION_SWARM,
    )
    t0 = time.time()
    sessions = run_solo(opt_root, iter_dir, spec, opt_model, timeout=lead_timeout)
    rec: dict = {
        "iter": it,
        "optimizer_status": "ok" if sessions["optimizer"]["rc"] == 0 else "codex_error",
        "optimizer_sec": round(time.time() - t0),
        "sessions": sessions,
        "candidate_made": candidate.exists(),
    }
    summary_file = iter_dir / "summary.json"
    if summary_file.exists():
        try:
            rec["result"] = json.loads(summary_file.read_text())
        except json.JSONDecodeError:
            rec["result"] = summary_file.read_text()[:2000]
    probe_rollouts = len(list((iter_dir / "probe").glob("*/*__r*"))) if (iter_dir / "probe").exists() else 0
    if probe_rollouts:
        rec["probe_rollouts"] = probe_rollouts
    if candidate.exists():
        ok, msg = validate_workflow(candidate)
        if not ok:  # the lead's own validation pass missed; an invalid candidate is a no-candidate round
            rec.update({"candidate_made": False, "validation_error": msg[:500]})
            print(f"  candidate REJECTED by validation gate: {msg[:200]}")
    return candidate, rec


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
    # Stagnation is "neither ledger moved": no frontier entry AND no task-book movement.
    # A round that claims an unsolved task or takes an owned task cheaper is real progress
    # even when its aggregate score enters no frontier — coverage is the search objective.
    recent_rounds = hist[-STAGNATION_ROUNDS:]
    if len(recent_rounds) == STAGNATION_ROUNDS and not any(
        h.get("entered_frontier") or h.get("book_moved") for h in recent_rounds
    ):
        fired.append(f"stagnation_{STAGNATION_ROUNDS}_rounds_no_frontier_or_book_movement")
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
    # "workflow": the pi _meta workflow optimizer (optimizer.js) with watchdog.
    # "codex": the codex trio (2 proposers + lead), the organization shared with the
    # meta-harness arm — no probes, watchdog disabled (predicates logged only).
    p.add_argument("--proposer", choices=["workflow", "codex"], default="workflow")
    # v9 protocol: contract-native search. Every candidate declares a manifest
    # (action + component list); the inventory ledger grows natively instead of
    # by post-hoc decomposition. Invalid manifest = no-candidate round.
    p.add_argument("--contract-native", action="store_true")
    # In-loop confirmation: tasks this round claimed/took over get this many extra
    # reps immediately (confirm/round_NNN/), keeping the book confirmed-grade as it
    # grows. 0 disables.
    p.add_argument("--confirm-repeats", type=int, default=2)
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

    if args.contract_native:
        init_inventory(opt_root)

    # Round 1 must already see the coverage block, so the book exists before the loop.
    book_path = opt_root / "task_book.json"
    if not book_path.exists():
        initial_book = task_book.build_book(opt_root)
        initial_book["domain"] = initial_book["domain"] or args.domain
        book_path.write_text(json.dumps(initial_book, indent=1, ensure_ascii=False))
        cov = initial_book["coverage"]
        print(f"task book initialised: union {cov['union']}/{cov['universe']}, "
              f"unsolved {len(cov['unsolved'])}")

    start_iter = (state["history"][-1]["iter"] + 1) if state["history"] else 1
    for it in range(start_iter, start_iter + args.iters):
        top = best_by_score(state["frontier"])
        print(f"=== iter {it} (frontier: {len(state['frontier'])} pts, best {top['score']:.3f} @ {top['tokens']:,} tok) ===")
        iter_dir = opt_root / f"iter_{it:03d}"
        (iter_dir / "opt").mkdir(parents=True, exist_ok=True)

        if args.proposer == "codex":
            candidate, rec = run_codex_round(opt_root, args.domain, it, state, iter_dir,
                                             args.opt_model, args.opt_max_sec, args.contract_native,
                                             args.eval_max_tokens, args.eval_max_sec)
            made = rec["candidate_made"]
            if made and args.contract_native:
                inventory = json.loads((opt_root / "lab/components.json").read_text())
                manifest = rec.get("result", {}).get("manifest") if isinstance(rec.get("result"), dict) else None
                manifest_errors = validate_manifest(manifest, inventory)
                if manifest_errors:
                    rec.update({"candidate_made": False, "manifest_error": "; ".join(manifest_errors)[:500]})
                    made = False
                    print(f"  candidate VOIDED by declaration protocol: {manifest_errors[:2]}")
        else:
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

                # The second ledger: rebuild the task book (pure function of graded
                # evidence on disk) and record what this candidate changed in it.
                book_path = opt_root / "task_book.json"
                old_book = json.loads(book_path.read_text()) if book_path.exists() else None
                book = task_book.build_book(opt_root)
                delta = task_book.diff_books(old_book, book)
                book_path.write_text(json.dumps(book, indent=1, ensure_ascii=False))
                book_moved = bool(delta["claimed"] or delta["took_over"])
                rec.update({"book_moved": book_moved, "book_delta": delta})
                if book_moved:
                    print(f"  book: claimed {delta['claimed'] or '[]'}, "
                          f"took over {[d['task'] for d in delta['took_over']] or '[]'}, "
                          f"union {delta['union']}")

                if args.contract_native and rec.get("candidate_made"):
                    inventory = json.loads((opt_root / "lab/components.json").read_text())
                    apply_manifest(inventory, f"iter_{it:03d}", rec["result"]["manifest"])
                    (opt_root / "lab/components.json").write_text(json.dumps(inventory, indent=1, ensure_ascii=False))

                # Winner's-curse control: a k=1 claim is provisional. Confirm this
                # round's newly won tasks immediately so the book stays honest.
                member = f"iter_{it:03d}"
                won = delta["claimed"] + [d["task"] for d in delta["took_over"] if d["to"] == member]
                if args.confirm_repeats > 0 and won:
                    confirm_dir = task_book.next_confirm_round(opt_root) / member
                    print(f"  confirming {len(won)} task(s) x {args.confirm_repeats} reps -> {confirm_dir.parent.name}")
                    subprocess.run(
                        ["python3", str(ROOT / "bench/owf_bench/core/runner.py"), "--domain", args.domain,
                         "--workflow", str(candidate), "--subset", "train", "--task-ids", ",".join(sorted(won)),
                         "--repeats", str(args.confirm_repeats), "--workers", str(min(len(won) * args.confirm_repeats, 16)),
                         "--out", str(confirm_dir), "--max-tokens", str(args.eval_max_tokens),
                         "--max-wallclock-sec", str(args.eval_max_sec)],
                        cwd=ROOT, capture_output=True, text=True, timeout=14400,
                        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "bench")},
                    )
                    book = task_book.build_book(opt_root)
                    book_path.write_text(json.dumps(book, indent=1, ensure_ascii=False))
                    cov = book["coverage"]
                    rec["confirm"] = {"tasks": sorted(won), "confirmed_after": cov["confirmed"]}
                    print(f"  confirmed coverage now {cov['confirmed']}/{cov['universe']}")
            else:
                rec.update({"candidate_score": None, "entered_frontier": False, "eval_failed": True})
        best = best_by_score(state["frontier"])
        rec["frontier_after"] = {"points": len(state["frontier"]), "best_score": best["score"], "best_tokens": best["tokens"]}
        state["history"].append(rec)
        state_path.write_text(json.dumps(state, indent=1))

        if args.proposer == "codex":
            # Watchdog disabled in trio mode (user decision, 2026-07-28): the optimizer is
            # driver code + prompts, not a rewritable workflow file. Predicates stay on
            # record so stalls and dead rounds remain visible in the log.
            predicates = compute_predicates(state)
            if predicates:
                print(f"  predicates fired (logged only, watchdog disabled): {predicates}")
        else:
            maybe_rollback(optimizer_path, state)
            predicates = compute_predicates(state)
            if predicates and it - state["last_watchdog_iter"] >= WATCHDOG_COOLDOWN:
                print(f"  predicates fired: {predicates} -> watchdog")
                run_watchdog(opt_root, args.domain, it, predicates, args.opt_model, optimizer_path, state)
                state_path.write_text(json.dumps(state, indent=1))

    # The champion still owes a k=3 confirmation and the held-out test set; the frontier
    # is measured at k=1 and picking its best point is exactly where luck accumulates.
    # Coverage counts carry the same caveat: `union` includes k=1 solves — only
    # `confirmed` is settled evidence (task_book.py --emit-confirm buys the rest).
    final_book = json.loads(book_path.read_text())
    print(json.dumps({"frontier": state["frontier"], "champion_by_score": best_by_score(state["frontier"]),
                      "coverage": final_book["coverage"], "cover_set": final_book["cover_set"],
                      "rounds": len(state["history"]), "watchdog_events": state["watchdog_events"]}, indent=1))


if __name__ == "__main__":
    main()
