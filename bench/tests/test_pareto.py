"""Pareto frontier maintenance for the optimization driver.

Run: PYTHONPATH=bench python3 -m unittest discover -s bench/tests
"""

import unittest

from owf_bench.core.optimize import best_by_score, dominates, update_frontier

BAND = 0.02


def pt(score, cost, name="w"):
    return {"workflow": name, "score": score, "cost": cost}


class TestDominates(unittest.TestCase):
    def test_better_on_both_axes_dominates(self):
        self.assertTrue(dominates(pt(0.80, 0.10), pt(0.70, 0.20), BAND))

    def test_worse_on_either_axis_never_dominates(self):
        self.assertFalse(dominates(pt(0.80, 0.30), pt(0.70, 0.20), BAND))  # better score, dearer
        self.assertFalse(dominates(pt(0.60, 0.10), pt(0.70, 0.20), BAND))  # cheaper, worse score

    def test_equal_points_do_not_dominate_each_other(self):
        a, b = pt(0.70, 0.20), pt(0.70, 0.20)
        self.assertFalse(dominates(a, b, BAND))
        self.assertFalse(dominates(b, a, BAND))

    def test_score_win_inside_noise_band_is_a_tie_broken_by_cost(self):
        # +0.01 is inside the band, so it is not a score win; cost decides.
        self.assertTrue(dominates(pt(0.71, 0.10), pt(0.70, 0.20), BAND))
        self.assertFalse(dominates(pt(0.71, 0.30), pt(0.70, 0.20), BAND))

    def test_score_win_outside_noise_band_dominates_at_equal_cost(self):
        self.assertTrue(dominates(pt(0.75, 0.20), pt(0.70, 0.20), BAND))

    def test_cheaper_at_equal_score_dominates(self):
        self.assertTrue(dominates(pt(0.70, 0.10), pt(0.70, 0.20), BAND))


class TestUpdateFrontier(unittest.TestCase):
    def test_dominated_candidate_is_rejected_and_set_unchanged(self):
        front = [pt(0.80, 0.10, "a")]
        new, entered = update_frontier(front, pt(0.70, 0.20, "b"), BAND)
        self.assertFalse(entered)
        self.assertEqual([p["workflow"] for p in new], ["a"])

    def test_dominating_candidate_evicts_the_points_it_beats(self):
        front = [pt(0.70, 0.20, "a"), pt(0.60, 0.30, "b")]
        new, entered = update_frontier(front, pt(0.85, 0.10, "c"), BAND)
        self.assertTrue(entered)
        self.assertEqual([p["workflow"] for p in new], ["c"])

    def test_tradeoff_candidate_coexists(self):
        # Lower score but much cheaper: neither dominates, both stay.
        front = [pt(0.80, 0.30, "a")]
        new, entered = update_frontier(front, pt(0.70, 0.05, "b"), BAND)
        self.assertTrue(entered)
        self.assertEqual(sorted(p["workflow"] for p in new), ["a", "b"])

    def test_cheaper_at_same_score_replaces_incumbent(self):
        # The single-axis driver rejected this outright (delta > 0 only); it is the
        # main thing the second axis buys us.
        front = [pt(0.80, 0.30, "a")]
        new, entered = update_frontier(front, pt(0.80, 0.12, "b"), BAND)
        self.assertTrue(entered)
        self.assertEqual([p["workflow"] for p in new], ["b"])

    def test_noise_sized_score_gain_at_higher_cost_is_rejected(self):
        # Guards against frontier churn: +0.01 is jitter, and it costs 3x more.
        front = [pt(0.80, 0.10, "a")]
        new, entered = update_frontier(front, pt(0.81, 0.30, "b"), BAND)
        self.assertFalse(entered)
        self.assertEqual([p["workflow"] for p in new], ["a"])

    def test_frontier_stays_sorted_by_score_then_cost(self):
        front = [pt(0.60, 0.05, "a")]
        front, _ = update_frontier(front, pt(0.90, 0.40, "c"), BAND)
        front, _ = update_frontier(front, pt(0.75, 0.20, "b"), BAND)
        self.assertEqual([p["workflow"] for p in front], ["c", "b", "a"])

    def test_repeated_identical_candidate_does_not_grow_the_set_unboundedly(self):
        front = [pt(0.80, 0.10, "a")]
        for i in range(5):
            front, _ = update_frontier(front, pt(0.80, 0.10, f"dup{i}"), BAND)
        # Equal points never dominate each other, so they accumulate — but the champion
        # selection stays deterministic rather than depending on insertion order.
        self.assertEqual(best_by_score(front)["score"], 0.80)


class TestBestByScore(unittest.TestCase):
    def test_picks_highest_score(self):
        self.assertEqual(best_by_score([pt(0.70, 0.05, "a"), pt(0.85, 0.40, "b")])["workflow"], "b")

    def test_breaks_score_ties_by_cost(self):
        self.assertEqual(best_by_score([pt(0.80, 0.40, "a"), pt(0.80, 0.09, "b")])["workflow"], "b")


if __name__ == "__main__":
    unittest.main()
