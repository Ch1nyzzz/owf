"""Shared optimizer organization for the two code-space arms: the codex trio.

One deterministic shape, driver-enforced, identical across arms by construction:

    score proposer (codex exec)  ┐  parallel
    token proposer (codex exec)  ┘
              │  proposals/*.md on disk
              ▼
    lead (codex exec) — reads both proposals + evidence, writes ONE candidate

The organization lives HERE, in code, not in a prompt asking a session to spawn
subagents: the optimizer organization is a controlled variable of the arm
comparison, so it must be a mechanism, not a behavior. Every hop lands on disk
(prompt, output, stderr per session) — the same evidence discipline as the
rollout journals.

Prompt text is shared verbatim between arms; each arm injects only its
representation contract (workflow DSL vs free agent module) and file paths.
No probes, no watchdog in this mode — both dropped for cross-arm symmetry.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ArmSpec:
    """What differs between arms: the representation, nothing else."""
    subject: str          # e.g. "an agent workflow (a workflow.js orchestration program)"
    rep_contract: str     # action space + candidate file format for this representation
    evidence_text: str    # frontier table, stability, gold, stalled hint, history paths
    candidate_path: Path  # where the lead must write the candidate
    validate_cmd: str     # shell command the lead runs until it passes
    lead_extra: str       # arm-specific output contract (e.g. pending_eval.json for meta)


PROPOSER_COMMON = """You are one of two proposers serving the optimizer of {subject}.
You do not write the candidate: you investigate the evidence and hand the lead ONE concrete,
implementable proposal for your assigned axis. Ground every claim in evidence you actually
read — cite task ids and journal paths. A proposal the lead cannot trace back to data is
worthless to it. Sample deliberately: a handful of representative rollouts read closely
beats skimming everything. Once the evidence supports one mechanism, stop reading and write.

{evidence_text}

{rep_contract}

Write EXACTLY one file: {proposal_path}
Markdown with sections:
- PARENT: which frontier point / earlier candidate you build on, and why
- GROUNDING: what the proposal rests on — the failure or waste pattern (task ids, journal
  paths), or the task-structure analysis the design follows from
- MECHANISM: the concrete change or organizational design
- CODE SKETCH: the fragment that implements it — enough for the lead to integrate verbatim
- EXPECTED: predicted score and token effect; for organizational designs, also the
  mechanism-level observables that would confirm the design (subgoal coverage, per-part
  context size, where the answer is assembled)
- RISK: what this could break, and what evidence would disconfirm it

Propose ONE mechanism, the one your grounding supports best — not a menu.
Modify no other file.

YOUR MANDATE THIS ROUND:
{axis_brief}
"""

REPAIR_BRIEF = """REFINE. You own the current organization's performance.
Take the frontier candidate as it is — its parts, connections and flow stay fixed — and make it
score higher and spend fewer tokens. Read the rollouts: what was asked, what each part
produced, what reached the final answer, and what the tokens were actually spent on. Check
whether a usable result already appeared somewhere in the trajectory and was lost on the way
out. Your instruments are the ones that leave the organization intact: each part's prompt, its
turn and budget allocation, its rails, what context it is handed, what it is asked to write.
Predict which tasks should flip and the token effect."""

DESIGN_BRIEF = """DESIGN. You own the organization.
A cooperative organization — a swarm — is a set of parts, each with its own context, role and
budget, connected by explicit flows of information: the task enters, the parts carry the work,
and everything converges into one delivered answer. The organization decides what each part
sees, what it is responsible for, and how its output reaches the rest.
Study this task's anatomy — the instructions, and trajectories of how the work actually
unfolds — and design the organization that fits it. Any topology the representation can
express is yours. Deliver ONE organizational design with a code sketch, and predict what it
changes at the mechanism level — each part's own subgoal, its context size, where the final
answer is assembled — alongside the expected movement on score and tokens."""

LEAD_PROMPT = """You are the optimizer of {subject}.
Your job this round: push the Pareto frontier for the target domain on two axes — score up,
tokens down. These are not ranked: a candidate that scores the same for half the tokens is as
real a win as one that scores higher.

{evidence_text}

TWO PROPOSALS are at proposals/repair_proposal.md and proposals/design_proposal.md, written by
proposers who each saw only their own mandate: one refines the current organization with its
shape held fixed (score up, tokens down), one designs the organization itself from the
anatomy of the task. A missing file means that proposer
died; proceed with what exists. They are evidence and argument, not orders. You ship ONE
candidate this round, and it is yours: adopt one, combine them where they compose cleanly, take
a mechanism from one and drop its integration, or reject both and do something the evidence
supports better. Verify a repair's citations with targeted reads before you build on it; judge
a design by its mechanism-level predictions — after evaluation, whether each part achieved its
own subgoal is a separate question from the total score, and a design whose mechanism works but
whose integration is rough is a parent worth refining, not a dead end.

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
  to redo it. Check the historical trajectories first — if the pattern repeats across tasks,
  the evidence is already there.
- Organization is the search space: how work is decomposed, who does what, what flows between
  parts, where results are verified and rescued. Explore organizational designs as freely as
  prompt wording — the explicit program representation exists to make cooperation designable.
- Before writing, put in NOTES.md: the failure pattern you target (cite tasks/journals), your
  hypothesis, and a concrete prediction (which tasks should flip, expected token change).
  NOTES.md is your only memory across rounds; you will re-read it cold next round. Curate it:
  beliefs with sources, hypotheses tried and their outcomes, prediction-vs-actual
  reconciliation from last round.

Hard interface:
- Write the candidate to: {candidate_path}
- Validate it with: {validate_cmd}
  Fix and re-validate until it prints OK. An invalid candidate is a wasted round.
{lead_extra}"""


def _artifact(prefix: Path, kind: str) -> Path:
    return prefix.parent / f"{prefix.name}.{kind}"


def run_codex(prompt: str, cwd: Path, prefix: Path, model: str, timeout: int) -> dict:
    """One codex exec session; prompt, last message and stderr all land next to `prefix`."""
    prefix.parent.mkdir(parents=True, exist_ok=True)
    _artifact(prefix, "prompt.md").write_text(prompt)
    out_file = _artifact(prefix, "out.md")
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["codex", "exec", "-m", model, "-c", 'model_reasoning_effort="xhigh"',
             "-s", "workspace-write", "--skip-git-repo-check",
             "--color", "never", "-o", str(out_file), "-"],
            input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        rc = proc.returncode
        _artifact(prefix, "stderr.txt").write_text(proc.stderr[-8000:])
    except subprocess.TimeoutExpired:
        rc = -1
    return {"rc": rc, "sec": round(time.time() - t0)}


def run_trio(workspace: Path, iter_dir: Path, spec: ArmSpec, model: str,
             propose_timeout: int = 3600, lead_timeout: int = 5400) -> dict:
    """Run one full optimization round: two parallel proposers, then the lead."""
    proposals_dir = iter_dir / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)

    sessions = {}
    procs = {}
    for axis, brief in (("repair", REPAIR_BRIEF), ("design", DESIGN_BRIEF)):
        proposal_path = proposals_dir / f"{axis}_proposal.md"
        prompt = PROPOSER_COMMON.format(subject=spec.subject, evidence_text=spec.evidence_text,
                                        rep_contract=spec.rep_contract,
                                        proposal_path=proposal_path, axis_brief=brief)
        prefix = iter_dir / f"propose_{axis}"
        _artifact(prefix, "prompt.md").write_text(prompt)
        out_file = _artifact(prefix, "out.md")
        stderr_file = _artifact(prefix, "stderr.txt").open("w")
        procs[axis] = (subprocess.Popen(
            ["codex", "exec", "-m", model, "-c", 'model_reasoning_effort="xhigh"',
             "-s", "workspace-write", "--skip-git-repo-check",
             "--color", "never", "-o", str(out_file), "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr_file, text=True,
            cwd=workspace,
        ), time.time(), stderr_file)
        procs[axis][0].stdin.write(prompt)
        procs[axis][0].stdin.close()

    for axis, (proc, t0, stderr_file) in procs.items():
        try:
            proc.wait(timeout=propose_timeout)
            sessions[f"propose_{axis}"] = {"rc": proc.returncode, "sec": round(time.time() - t0)}
        except subprocess.TimeoutExpired:
            proc.kill()
            sessions[f"propose_{axis}"] = {"rc": -1, "sec": round(time.time() - t0)}
        finally:
            stderr_file.close()

    lead_prompt = LEAD_PROMPT.format(subject=spec.subject, evidence_text=spec.evidence_text,
                                     rep_contract=spec.rep_contract,
                                     candidate_path=spec.candidate_path,
                                     validate_cmd=spec.validate_cmd, lead_extra=spec.lead_extra)
    sessions["lead"] = run_codex(lead_prompt, workspace, iter_dir / "lead", model, lead_timeout)
    return sessions
