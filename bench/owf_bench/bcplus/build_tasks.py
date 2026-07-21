"""Build the owf bcplus task set from the decrypted BrowseComp-Plus queries.

Source: data/bcplus/browsecomp_plus_decrypted.jsonl (830 queries; texttron,
ACL 2026, arXiv:2508.06600). Gold short answers stay bench-side only.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "data/bcplus/browsecomp_plus_decrypted.jsonl"
OUT_DIR = ROOT / "data/bcplus"
SEED = 1729
TRAIN_FRACTION = 0.3

NOTE = (
    "\n\nYou search a fixed local corpus (not the live web) via the `search` tool; "
    "read promising documents in full with `open_doc`. Answer with the exact entity/value asked for."
)


def main() -> None:
    tasks = []
    for line in SOURCE.read_text().splitlines():
        r = json.loads(line)
        tasks.append({"id": f"bcp-{r['query_id']}", "instruction": r["query"] + NOTE, "gold": r["answer"]})

    with (OUT_DIR / "tasks.jsonl").open("w") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    ids = sorted(t["id"] for t in tasks)
    rng = random.Random(SEED)
    rng.shuffle(ids)
    n = round(len(ids) * TRAIN_FRACTION)
    split = {
        "_provenance": f"seeded shuffle seed={SEED}, train_fraction={TRAIN_FRACTION}, 830 decrypted queries",
        "train": sorted(ids[:n]),
        "test": sorted(ids[n:]),
    }
    (OUT_DIR / "split.json").write_text(json.dumps(split, indent=1))
    print(f"wrote {len(tasks)} tasks; split train={n} test={len(ids) - n}")


if __name__ == "__main__":
    main()
