"""Shared single-session optimizer for the two code-space arms.

One codex session per iteration IS the optimizer: it reads the evidence,
writes the candidate, and validates it — the shape of the original
meta-harness loop (arXiv 2603.28052: one agentic proposer session per
iteration over a filesystem of prior candidates, scores and traces, guided
by a minimal skill that says where to write candidates, how to inspect
history, and what is off-limits).

The prompt skeleton is shared verbatim between arms. Each arm injects its
representation contract, evidence layout and output interface — and ONE
experimental variable: `orientation`. The baseline arm (meta_loop.py)
leaves it empty, staying paper-faithful and unsteered. The main arm
(optimize.py --proposer codex) injects ORIENTATION_SWARM, a standing
preference for designing swarm collaboration schemes. That paragraph is
the difference under test.

All sessions run at model_reasoning_effort=xhigh.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ArmSpec:
    """What differs between arms: representation, interface, and the orientation block."""
    subject: str          # e.g. "an agent workflow (a workflow.js orchestration program)"
    rep_contract: str     # action space + candidate file format for this representation
    evidence_text: str    # frontier table, stability, gold, history paths
    candidate_path: Path  # where the candidate must be written
    validate_cmd: str     # shell command to run until it passes
    extra: str            # arm-specific output contract (summary.json / pending_eval.json)
    orientation: str = ""  # the experimental variable: "" (baseline) or ORIENTATION_SWARM


# The main arm's single deviation from the baseline prompt: a standing preference for
# designing the organization of the work as a cooperating swarm. Everything else in the
# session — evidence, budgets, gates, output contract — is shared.
ORIENTATION_SWARM = """
Design orientation — this arm designs swarm collaboration schemes:
Prefer working at the level of the ORGANIZATION of the work. A cooperative organization — a
swarm — is a set of parts, each with its own context, role and budget, connected by explicit
flows of information: the task enters, the parts carry the work, and everything converges into
one delivered answer. The organization decides what each part sees, what it is responsible
for, and how its output reaches the rest. Study this task's anatomy — the instructions, and
trajectories of how the work actually unfolds — and design the collaboration scheme that fits
it; once a designed organization is on the frontier, refining its parts (per-part prompts,
context handoffs, budgets, rails) is organization design too. Any topology the representation
can express is yours.
"""

SOLO_PROMPT = """You are the optimizer of {subject}.
Your job this round: push the Pareto frontier for the target domain on two axes — score up,
tokens down. These are not ranked: a candidate that scores the same for half the tokens is as
real a win as one that scores higher. You ship exactly ONE candidate this round.

{evidence_text}
{orientation}
{rep_contract}

Ground rules:
- The candidate must be a GENERAL program: no branching on task ids, no lookup tables of known
  answers, no rules fitted to individual problems.
- Parent choice is yours: any frontier point or any earlier candidate — rejected candidates
  often contain good ideas that did not clear the bar alone. Start from a copy of any prior
  candidate, graft across several, or write from scratch.
- NOTES.md persists across rounds; use it however you find useful.

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
                                orientation=spec.orientation, rep_contract=spec.rep_contract,
                                candidate_path=spec.candidate_path,
                                validate_cmd=spec.validate_cmd, extra=spec.extra)
    return {"optimizer": run_codex(prompt, workspace, iter_dir / "optimizer", model, timeout)}
