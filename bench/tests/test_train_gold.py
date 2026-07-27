"""Train-gold evidence generation: train ids only, test split sealed.

Run: PYTHONPATH=bench python3 -m unittest discover -s bench/tests
"""

import json
import tempfile
import unittest
from pathlib import Path

from owf_bench.core.optimize import write_train_gold


def make_data_root(root: Path, domain: str, tasks: list[dict], split: dict) -> Path:
    data_root = root / "data"
    d = data_root / domain
    d.mkdir(parents=True)
    (d / "tasks.jsonl").write_text("\n".join(json.dumps(t) for t in tasks) + "\n")
    (d / "split.json").write_text(json.dumps(split))
    return data_root


class TestWriteTrainGold(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.opt_root = self.root / "opt"
        (self.opt_root / "evidence").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_train_ids_only_and_gold_verbatim(self):
        tasks = [
            {"id": "t1", "instruction": "a", "gold": ["1", "2"]},
            {"id": "t2", "instruction": "b", "gold": "answer-2"},
            {"id": "t3", "instruction": "c", "gold": "SEALED"},
        ]
        split = {"_provenance": "test", "train": ["t1", "t2"], "test": ["t3"]}
        data_root = make_data_root(self.root, "dom", tasks, split)

        write_train_gold(self.opt_root, "dom", data_root)

        out = json.loads((self.opt_root / "evidence/train_gold.json").read_text())
        self.assertEqual(out, {"t1": ["1", "2"], "t2": "answer-2"})
        self.assertNotIn("SEALED", (self.opt_root / "evidence/train_gold.json").read_text())

    def test_missing_data_files_write_nothing(self):
        write_train_gold(self.opt_root, "bridged-domain", self.root / "data")
        self.assertFalse((self.opt_root / "evidence/train_gold.json").exists())

    def test_task_without_gold_is_skipped(self):
        tasks = [
            {"id": "t1", "instruction": "a", "gold": "g1"},
            {"id": "t2", "instruction": "b"},
        ]
        split = {"train": ["t1", "t2"], "test": []}
        data_root = make_data_root(self.root, "dom", tasks, split)

        write_train_gold(self.opt_root, "dom", data_root)

        out = json.loads((self.opt_root / "evidence/train_gold.json").read_text())
        self.assertEqual(out, {"t1": "g1"})


if __name__ == "__main__":
    unittest.main()
