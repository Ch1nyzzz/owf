"""Component attribution estimators (distill stage A).

Run: PYTHONPATH=bench python3 -m unittest discover -s bench/tests
"""

import unittest

from owf_bench.core.distill import (
    pair_contrast,
    param_pairs,
    single_diff_pairs,
    with_vs_without,
)

LAB = {
    "components": {"a": {}, "b": {}},
    "members": {
        "m0": {},                 # baseline
        "m1": {"a": 1},           # +a
        "m2": {"a": 1, "b": 1},   # +a+b
        "m3": {"a": 1, "b": 1},   # same set as m2 (param variant)
    },
}
OUT = {
    "t1": {"m0": 0.0, "m1": 1.0, "m2": 1.0, "m3": 0.0},
    "t2": {"m0": 1.0, "m1": 1.0, "m2": 0.0, "m3": 0.0},
}


class TestEstimators(unittest.TestCase):
    def test_with_vs_without_delta(self):
        wvw = with_vs_without(LAB, OUT)
        # component a on t1: carriers m1,m2,m3 mean 2/3 vs m0 0.0 -> +0.6667
        self.assertAlmostEqual(wvw["a"]["per_task"]["t1"]["delta"], 0.6667, places=3)
        # component b on t2: m2,m3 mean 0 vs m0,m1 mean 1 -> -1.0
        self.assertAlmostEqual(wvw["b"]["per_task"]["t2"]["delta"], -1.0)

    def test_single_diff_pairs_finds_exact_one_component_contrasts(self):
        pairs = single_diff_pairs(LAB)
        self.assertEqual(pairs["a"], [("m0", "m1")])
        # b: m1 vs m2 AND m1 vs m3 (both differ from m1 by exactly {b})
        self.assertEqual(sorted(pairs["b"]), [("m1", "m2"), ("m1", "m3")])

    def test_param_pairs_are_identical_sets(self):
        self.assertEqual(param_pairs(LAB), [("m2", "m3")])

    def test_pair_contrast_counts_flips(self):
        c = pair_contrast(("m0", "m1"), OUT)
        self.assertEqual((c["wins"], c["losses"]), (1, 0))
        self.assertEqual(c["flips"], {"t1": 1.0})


if __name__ == "__main__":
    unittest.main()
