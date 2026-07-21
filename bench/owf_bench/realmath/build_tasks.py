"""Build the owf realmath task set from the SageMath-agents normalized pool.

Source: data/sagemath-agents/data/processed/normalized_realmath.json
(219 SymPy-verifiable problems filtered from RealMath, arXiv:2607.06820 pipeline;
RealMath itself: ethz-spylab, arXiv:2505.12575).

Outputs (committed):
  data/realmath/tasks.jsonl  — one task per line: {id, instruction, answer_kind, gold}
  data/realmath/split.json   — seeded 30% train / 70% test split
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "data/sagemath-agents/data/processed/normalized_realmath.json"
OUT_DIR = ROOT / "data/realmath"
SEED = 1729
TRAIN_FRACTION = 0.3

ANSWER_FORMAT_NOTE = (
    "\n\nAnswer format: submit via the submit_result tool with `answer` as a list of strings, "
    "each a SymPy-parseable expression (e.g. [\"3/4\"], [\"sqrt(2)\"], [\"1\", \"2\"] for multi-part). "
    "Use exact symbolic form, not decimal approximations."
)


def main() -> None:
    rows = json.loads(SOURCE.read_text())
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    tasks = []
    for row in rows:
        tasks.append(
            {
                "id": row["id"],
                "instruction": row["question"] + ANSWER_FORMAT_NOTE,
                "answer_kind": row["answer_kind"],
                "gold": row["sympy_answer"],
            }
        )

    with (OUT_DIR / "tasks.jsonl").open("w") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    ids = sorted(t["id"] for t in tasks)
    rng = random.Random(SEED)
    rng.shuffle(ids)
    n_train = round(len(ids) * TRAIN_FRACTION)
    split = {
        "_provenance": f"seeded shuffle seed={SEED}, train_fraction={TRAIN_FRACTION}, source=normalized_realmath.json (219)",
        "train": sorted(ids[:n_train]),
        "test": sorted(ids[n_train:]),
    }
    (OUT_DIR / "split.json").write_text(json.dumps(split, indent=1))
    print(f"wrote {len(tasks)} tasks; split train={n_train} test={len(ids) - n_train}")


if __name__ == "__main__":
    main()
