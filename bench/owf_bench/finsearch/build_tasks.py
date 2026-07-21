"""Build the owf finsearch task set: FinSearchComp T2+T3 only (static answers).

Source: data/finsearch-repo/data/finsearchcomp_data.json (635 tasks; ByteDance
Seed, arXiv:2509.13160). T1 Time-Sensitive is excluded — its gold drifts with
live markets. Each task carries its official per-task judge prompts.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "data/finsearch-repo/data/finsearchcomp_data.json"
OUT_DIR = ROOT / "data/finsearch"
SEED = 1729
TRAIN_FRACTION = 0.3


def main() -> None:
    rows = json.loads(SOURCE.read_text())
    keep = [r for r in rows if "Simple_Historical_Lookup" in r["label"] or "Complex_Historical_Investigation" in r["label"]]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    tasks = []
    for r in keep:
        tasks.append(
            {
                "id": r["prompt_id"],
                "instruction": r["prompt"],
                "label": r["label"],
                "gold": r["response_reference"],
                "judge_system": r["judge_system_prompt"],
                "judge_template": r["judge_prompt_template"],
            }
        )

    with (OUT_DIR / "tasks.jsonl").open("w") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # stratified split: shuffle within each label bucket
    rng = random.Random(SEED)
    train: list[str] = []
    test: list[str] = []
    by_label: dict[str, list[str]] = {}
    for t in tasks:
        by_label.setdefault(t["label"], []).append(t["id"])
    for label in sorted(by_label):
        ids = sorted(by_label[label])
        rng.shuffle(ids)
        n = round(len(ids) * TRAIN_FRACTION)
        train += ids[:n]
        test += ids[n:]

    split = {
        "_provenance": f"stratified by label, seed={SEED}, train_fraction={TRAIN_FRACTION}, T2+T3 only ({len(tasks)} of 635)",
        "train": sorted(train),
        "test": sorted(test),
    }
    (OUT_DIR / "split.json").write_text(json.dumps(split, indent=1, ensure_ascii=False))
    print(f"wrote {len(tasks)} tasks; split train={len(train)} test={len(test)}")


if __name__ == "__main__":
    main()
