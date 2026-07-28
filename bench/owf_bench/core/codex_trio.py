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
- DIAGNOSIS: the failure or waste pattern, with task ids and journal paths
- MECHANISM: the concrete change
- CODE SKETCH: the fragment that implements it — enough for the lead to integrate verbatim
- EXPECTED: predicted score and token effect, and which tasks should move
- RISK: what this could break, and what evidence would disconfirm it

Propose ONE mechanism, the one your evidence supports best — not a menu.
Modify no other file.

YOUR AXIS THIS ROUND:
{axis_brief}
"""

SCORE_BRIEF = """RAISE THE SCORE. You own the accuracy axis and nothing else.
Start from the highest-scoring frontier point unless the evidence argues for another parent.
Find tasks that are failing and could be made to pass: read what the rollouts actually answered
and why it was wrong or absent. Check whether a usable result already appeared somewhere in the
trajectory and was lost on the way to the final answer — repairing that path is a different fix
from making a node redo the work, and usually a cheaper one. Then propose the structure, prompt,
routing or rail that would fix that failure MODE on unseen tasks of the same kind.
Extra tokens are allowed when they buy accuracy, but say what they buy: name the mechanism the
spend funds. Do NOT propose a change whose main effect is saving tokens — that is the other
proposer's job, and a round where both of you optimise cost is a wasted round."""

TOKEN_BRIEF = """CUT THE TOKENS AT UNCHANGED SCORE. You own the cost axis and nothing else.
Start from the leanest frontier point unless the evidence argues for another parent.
Find waste, not work: turns that repeat themselves, output nobody reads, context handed to a
step that does not need it, retries that never change the answer, verbose instructions that buy
nothing. Read the journals to distinguish a turn that changed the outcome from a turn that
merely happened. The score must hold. A change that saves tokens by giving up answers is a
regression, not a win — if your mechanism risks losing a correct answer, say so in RISK and
explain why the evidence says it will not."""

LEAD_PROMPT = """You are the optimizer of {subject}.
Your job this round: push the Pareto frontier for the target domain on two axes — score up,
tokens down. These are not ranked: a candidate that scores the same for half the tokens is as
real a win as one that scores higher.

{evidence_text}

TWO PROPOSALS are at proposals/score_proposal.md and proposals/token_proposal.md, one per axis,
written by proposers who each saw only their own mandate. A missing file means that proposer
died; proceed with what exists. They are evidence and argument, not orders. You ship ONE
candidate this round, and it is yours: adopt one, combine them where they compose cleanly, take
a mechanism from one and drop its integration, or reject both and do something the evidence
supports better. Verify their citations with targeted reads before you build on them. Two
mechanisms that both touch the same part usually conflict; prefer shipping one cleanly over
merging both badly.

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
            ["codex", "exec", "-m", model, "-s", "workspace-write", "--skip-git-repo-check",
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
    for axis, brief in (("score", SCORE_BRIEF), ("tokens", TOKEN_BRIEF)):
        proposal_path = proposals_dir / f"{'score' if axis == 'score' else 'token'}_proposal.md"
        prompt = PROPOSER_COMMON.format(subject=spec.subject, evidence_text=spec.evidence_text,
                                        rep_contract=spec.rep_contract,
                                        proposal_path=proposal_path, axis_brief=brief)
        prefix = iter_dir / f"propose_{axis}"
        _artifact(prefix, "prompt.md").write_text(prompt)
        out_file = _artifact(prefix, "out.md")
        stderr_file = _artifact(prefix, "stderr.txt").open("w")
        procs[axis] = (subprocess.Popen(
            ["codex", "exec", "-m", model, "-s", "workspace-write", "--skip-git-repo-check",
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
