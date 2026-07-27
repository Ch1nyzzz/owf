"""GEPA adapter over the owf bench: the prompt-only baseline arm.

The arm answers one question — how far does the SAME evaluation budget go when
the action space is exactly one system-prompt string? Everything else is pinned
to the main arm's protocol: same seed structure (the candidate template is the
parity seed with only the static system text swappable), same runner, same
train split, same per-rollout budget, same SUT model.

GEPA sees rich per-example feedback (submitted vs gold, grader verdict, a
transcript tail), i.e. it is trace-informed like the main arm; what it cannot
do is change topology, tools, rails, or budgets. The delta between the two arms
is therefore attributable to the organizational action space.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gepa.core.adapter import EvaluationBatch

ROOT = Path(__file__).resolve().parents[3]

# Seed candidates: the static text of each domain's parity seed, verbatim.
# realmath's seed interleaves a per-task multi-part notice MID-prompt (a conditional on
# a task field, inexpressible in a static string), so its candidate is TWO components —
# head and tail around the notice — keeping the seed byte-identical to the workflow
# seed. GEPA candidates are Dict[component -> text]; it mutates them independently.
SEED_PROMPTS = {
    "realmath": {
        "system_head": (
            "You are a research mathematician. Solve the problem exactly. "
            "Use the python tool (sympy is available) to compute and verify your answer numerically or symbolically before submitting. "
            "Submit exact symbolic expressions (SymPy-parseable strings), never decimal approximations."
        ),
        "system_tail": (
            "Keep written reasoning brief; put computations in python. "
            "If you cannot fully verify, still submit your best answer before running out of turns."
        ),
    },
    "bcplus": {
        "system_prompt": (
            "You are a persistent research agent working over a fixed document corpus. "
            "The answer exists in the corpus. Decompose the clues, search with varied keyword combinations "
            "(entities, dates, rare phrases), and open promising documents to verify every criterion. "
            "If a search returns nothing useful, reformulate rather than give up. "
            "You have a limited number of turns: track how many you have used, and once you are running low, "
            "stop searching and commit to your best current candidate — an unverified best guess scores far "
            "better than no answer at all. Never end without an answer. "
            "End with the exact answer as: Final answer: <answer>."
        ),
    },
}

# Candidate templates: byte-identical to workflows/<domain>/seed_parity.js except the
# static system text, injected as a JS string literal via json.dumps.
TEMPLATES = {
    "realmath": """export const meta = { name: 'realmath-gepa-candidate', version: 1 }

export default async function run(ctx) {
  const parts = ctx.task.answer_kind === 'multi' ? 'This problem has MULTIPLE answer parts; submit one string per part, in order.' : 'Submit a single-element list.'
  const out = await ctx.agent(ctx.task.instruction, {
    system: __GEPA_system_head__ + ' ' + parts + ' ' + __GEPA_system_tail__,
    model: 'deepseek-v4-flash',
    tools: ['python'],
    maxTurns: 64,
    schema: {
      type: 'object',
      properties: { answer: { type: 'array', items: { type: 'string' } } },
      required: ['answer'],
    },
    label: 'solve',
  })
  return { answer: out ? out.answer : null }
}
""",
    "bcplus": """export const meta = { name: 'bcplus-gepa-candidate', version: 1 }

export default async function run(ctx) {
  const out = await ctx.agent(ctx.task.instruction, {
    system: __GEPA_system_prompt__,
    model: 'deepseek-v4-flash',
    tools: ['search', 'open_doc'],
    maxTurns: 64,
    label: 'searcher',
  })
  return { answer: out }
}
""",
}

TRANSCRIPT_TAIL_CHARS = 2000


@dataclass
class OwfGEPAAdapter:
    domain: str
    out_root: Path
    max_tokens: int = 600_000
    max_sec: int = 1800
    workers: int = 16
    limit: int | None = None  # cap trainset size (smoke runs)
    # Canonical seed eval dir — the shared iteration 0. When set, evaluating the
    # unmutated seed candidate serves scores/outputs/trajectories from this run
    # instead of re-rolling: every arm evolves from the SAME seed measurement.
    baseline_run: Path | None = None

    # Optional GEPAAdapter protocol member: the engine probes this attribute directly,
    # and a plain class that omits it raises AttributeError. None selects the default
    # instruction-proposal path.
    propose_new_texts = None

    def __post_init__(self):
        self.out_root = Path(self.out_root)
        (self.out_root / "candidates").mkdir(parents=True, exist_ok=True)
        (self.out_root / "evals").mkdir(parents=True, exist_ok=True)
        data_dir = ROOT / "data" / self.domain
        split = json.loads((data_dir / "split.json").read_text())
        train_ids = set(split["train"])
        tasks = [json.loads(l) for l in (data_dir / "tasks.jsonl").read_text().splitlines() if l.strip()]
        self.trainset = sorted((t for t in tasks if t["id"] in train_ids), key=lambda t: t["id"])
        if self.limit:
            self.trainset = self.trainset[: self.limit]
        self._call_seq = 0

    # -- GEPAAdapter interface ------------------------------------------------

    def evaluate(self, batch, candidate, capture_traces=False):
        if self.baseline_run is not None and candidate == SEED_PROMPTS[self.domain]:
            return self._serve_seed_from_canonical(batch, capture_traces)
        wf = self._write_candidate(candidate)
        self._call_seq += 1
        eval_dir = self.out_root / "evals" / f"{self._call_seq:04d}_{wf.stem.split('_')[-1]}"
        ids = [t["id"] for t in batch]
        cmd = [
            "python3", str(ROOT / "bench/owf_bench/core/runner.py"),
            "--domain", self.domain, "--workflow", str(wf),
            "--subset", "train", "--task-ids", ",".join(ids),
            "--repeats", "1", "--workers", str(min(self.workers, len(ids))),
            "--out", str(eval_dir),
            "--max-tokens", str(self.max_tokens), "--max-wallclock-sec", str(self.max_sec),
        ]
        import os
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=14400,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "bench")})
        results_file = eval_dir / "results.jsonl"
        if not results_file.exists():
            raise RuntimeError(f"runner produced no results in {eval_dir}: {proc.stderr[-800:]}")
        by_id = {r["task_id"]: r for l in results_file.read_text().splitlines() if l.strip()
                 for r in [json.loads(l)]}

        outputs, scores, trajectories = [], [], []
        for task in batch:
            rec = by_id.get(task["id"], {"score": 0.0, "status": "missing"})
            answer = self._full_answer(eval_dir, task["id"])
            outputs.append({"task_id": task["id"], "answer": answer,
                            "status": rec.get("status"), "tokens": rec.get("tokens")})
            scores.append(float(rec.get("score", 0.0)))
            if capture_traces:
                trajectories.append({
                    "task": task, "record": rec, "answer": answer,
                    "transcript_tail": self._transcript_tail(eval_dir, task["id"]),
                })
        return EvaluationBatch(outputs=outputs, scores=scores,
                               trajectories=trajectories if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        records = []
        for traj, score in zip(eval_batch.trajectories, eval_batch.scores):
            task, rec = traj["task"], traj["record"]
            if score > 0:
                feedback = "Correct."
            else:
                feedback = (
                    f"Wrong or missing. Submitted: {json.dumps(traj['answer'], ensure_ascii=False)[:500]}. "
                    f"Expected (gold): {json.dumps(task.get('gold'), ensure_ascii=False)[:500]}. "
                    f"Grader verdict: {rec.get('match_type', rec.get('status'))}; rollout status: {rec.get('status')}; "
                    f"tokens: {rec.get('tokens')}.\n"
                    f"End of the solver transcript:\n{traj['transcript_tail']}"
                )
            records.append({
                "Inputs": task["instruction"][:2000],
                "Generated Outputs": json.dumps(traj["answer"], ensure_ascii=False)[:500],
                "Feedback": feedback[:4000],
            })
        return {c: records for c in components_to_update}

    def _serve_seed_from_canonical(self, batch, capture_traces: bool):
        """The seed's evaluation IS the canonical baseline run — replayed, not re-rolled."""
        report = json.loads((self.baseline_run / "report.json").read_text())
        task_scores = report["task_scores"]
        outputs, scores, trajectories = [], [], []
        for task in batch:
            answer = self._full_answer(self.baseline_run, task["id"])
            outputs.append({"task_id": task["id"], "answer": answer, "status": "canonical"})
            scores.append(float(task_scores[task["id"]]))
            if capture_traces:
                trajectories.append({
                    "task": task,
                    "record": {"status": "canonical", "match_type": "canonical baseline"},
                    "answer": answer,
                    "transcript_tail": self._transcript_tail(self.baseline_run, task["id"]),
                })
        return EvaluationBatch(outputs=outputs, scores=scores,
                               trajectories=trajectories if capture_traces else None)

    # -- helpers --------------------------------------------------------------

    def _write_candidate(self, candidate: dict[str, str]) -> Path:
        digest = hashlib.sha1(json.dumps(candidate, sort_keys=True).encode()).hexdigest()[:10]
        wf = self.out_root / "candidates" / f"candidate_{digest}.js"
        if not wf.exists():
            src = TEMPLATES[self.domain]
            for component, text in candidate.items():
                src = src.replace(f"__GEPA_{component}__", json.dumps(text))
            wf.write_text(src)
        return wf

    def _full_answer(self, eval_dir: Path, task_id: str):
        result_file = eval_dir / f"{task_id}__r0" / "result.json"
        if not result_file.exists():
            return None
        summary = json.loads(result_file.read_text())
        result = summary.get("result")
        return result.get("answer") if isinstance(result, dict) else result

    def _transcript_tail(self, eval_dir: Path, task_id: str) -> str:
        run_dir = eval_dir / f"{task_id}__r0"
        if not run_dir.exists():
            return "(no rollout dir)"
        transcripts = sorted(run_dir.glob("node-*.jsonl"))
        if not transcripts:
            return "(no transcript)"
        text = transcripts[-1].read_text()
        return text[-TRANSCRIPT_TAIL_CHARS:]
