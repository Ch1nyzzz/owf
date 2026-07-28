"""Shared single-session optimizer for the two code-space arms.

One codex session per iteration IS the optimizer: it reads the evidence,
chooses its level of intervention, writes the candidate, and validates it.
This replaces the proposer/lead trio — with a single proposal there is
nothing to integrate, and every extra hop is a handoff tax. It is also the
shape of the original meta-harness loop (one proposer session per iteration),
so the baseline arm regains protocol fidelity for free.

Two kinds of move are always open and live in one prompt: REFINE (the current
organization, shape fixed) and REDESIGN (the organization itself, from the
task's anatomy). Which level a round works at is the optimizer's call, guided
by the evidence — the stalled-frontier line in the evidence text is the
standing signal that refinement has stopped paying.

Prompt text is shared verbatim between arms; each arm injects only its
representation contract, evidence layout, and output interface. All sessions
run at model_reasoning_effort=xhigh.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ArmSpec:
    """What differs between arms: the representation and the interface, nothing else."""
    subject: str          # e.g. "an agent workflow (a workflow.js orchestration program)"
    rep_contract: str     # action space + candidate file format for this representation
    evidence_text: str    # frontier table, stability, gold, stalled hint, history paths
    candidate_path: Path  # where the candidate must be written
    validate_cmd: str     # shell command to run until it passes
    extra: str            # arm-specific output contract (summary.json / pending_eval.json)


SOLO_PROMPT = """You are the optimizer of {subject}.
Your job this round: push the Pareto frontier for the target domain on two axes — score up,
tokens down. These are not ranked: a candidate that scores the same for half the tokens is as
real a win as one that scores higher. You ship exactly ONE candidate this round.

{evidence_text}

Two kinds of move are always open to you; the evidence decides which level this round works at:

- REFINE the current organization, shape held fixed: each part's prompt, its turn and budget
  allocation, its rails, what context it is handed, what it is asked to write. Read the
  rollouts — what was asked, what each part produced, what reached the final answer, what the
  tokens were spent on, and whether a usable result appeared somewhere and was lost on the way
  out.

- REDESIGN the organization itself. A cooperative organization — a swarm — is a set of parts,
  each with its own context, role and budget, connected by explicit flows of information: the
  task enters, the parts carry the work, and everything converges into one delivered answer.
  The organization decides what each part sees, what it is responsible for, and how its output
  reaches the rest. Study this task's anatomy — the instructions, and trajectories of how the
  work actually unfolds — and design the organization that fits it. Any topology the
  representation can express is yours.

{rep_contract}

How to work:
- The candidate must be a GENERAL program: no branching on task ids, no lookup tables of known
  answers, no rules fitted to individual problems. Per-task evidence is for inferring the
  failure MODE; the fix ships as structure, prompts, routing, or rails that would help an
  unseen task of that kind.
- Evidence before rules: any question answerable by reading data must be answered that way.
- Failures have two possible shapes: an existing part did its job badly, OR a needed part does
  not exist. Consider both, and put one counterfactual to every failure cluster: did the
  execution ever contain enough useful work to succeed? If no — the gap is capability or
  coverage. If yes — trace why that work did not reach the final answer before asking any part
  to redo it.
- A structural change is judged at the mechanism level: whether each part achieved its OWN
  subgoal is a separate question from the total score. A design whose mechanism works but whose
  integration is rough is a parent worth refining, not a dead end.
- Before writing, put in NOTES.md: what you are targeting (cite tasks/journals or the
  task-structure analysis), your hypothesis, and a concrete prediction — which tasks should
  flip and the expected token change for a refinement; the mechanism-level observables (each
  part's subgoal, its context size, where the answer is assembled) for a redesign. NOTES.md is
  your only memory across rounds; you will re-read it cold next round. Curate it: beliefs with
  sources, hypotheses tried and their outcomes, prediction-vs-actual reconciliation.

Hard interface:
- Write the candidate to: {candidate_path}
- Validate it with: {validate_cmd}
  Fix and re-validate until it prints OK. An invalid candidate is a wasted round.
{extra}"""


def run_codex(prompt: str, cwd: Path, prefix: Path, model: str, timeout: int) -> dict:
    """One codex exec session; prompt, last message and stderr all land next to `prefix`."""
    prefix.parent.mkdir(parents=True, exist_ok=True)
    (prefix.parent / f"{prefix.name}.prompt.md").write_text(prompt)
    out_file = prefix.parent / f"{prefix.name}.out.md"
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["codex", "exec", "-m", model, "-c", 'model_reasoning_effort="xhigh"',
             "-s", "workspace-write", "--skip-git-repo-check",
             "--color", "never", "-o", str(out_file), "-"],
            input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        rc = proc.returncode
        (prefix.parent / f"{prefix.name}.stderr.txt").write_text(proc.stderr[-8000:])
    except subprocess.TimeoutExpired:
        rc = -1
    return {"rc": rc, "sec": round(time.time() - t0)}


def run_solo(workspace: Path, iter_dir: Path, spec: ArmSpec, model: str,
             timeout: int = 5400) -> dict:
    """Run one full optimization round: a single optimizer session."""
    prompt = SOLO_PROMPT.format(subject=spec.subject, evidence_text=spec.evidence_text,
                                rep_contract=spec.rep_contract,
                                candidate_path=spec.candidate_path,
                                validate_cmd=spec.validate_cmd, extra=spec.extra)
    return {"optimizer": run_codex(prompt, workspace, iter_dir / "optimizer", model, timeout)}
