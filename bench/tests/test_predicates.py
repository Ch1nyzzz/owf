"""Health predicates that decide when the driver wakes the watchdog.

Run: PYTHONPATH=bench python3 -m unittest discover -s bench/tests
"""

import unittest

from owf_bench.core.optimize import (
    EXPLORE_HINT_ROUNDS,
    NO_CANDIDATE_STREAK,
    STAGNATION_ROUNDS,
    compute_predicates,
    stalled_rounds,
)


def rnd(it, *, status="ok", made=True, entered=True, result=None):
    """One history record, healthy by default; override the field under test."""
    return {
        "iter": it,
        "optimizer_status": status,
        "candidate_made": made,
        "entered_frontier": entered,
        "result": result,
    }


def state(history):
    return {"history": history}


class TestQuietRounds(unittest.TestCase):
    def test_healthy_history_fires_nothing(self):
        self.assertEqual(compute_predicates(state([rnd(i) for i in range(1, 8)])), [])

    def test_empty_history_fires_nothing(self):
        self.assertEqual(compute_predicates(state([])), [])


class TestOptimizerStatus(unittest.TestCase):
    def test_budget_exceeded_fires_on_the_first_occurrence(self):
        fired = compute_predicates(state([rnd(1), rnd(2, status="budget_exceeded")]))
        self.assertIn("optimizer_budget_exceeded", fired)

    def test_only_the_latest_round_is_consulted(self):
        # A recovered run should not keep re-firing on last round's failure.
        fired = compute_predicates(state([rnd(1, status="timeout"), rnd(2)]))
        self.assertNotIn("optimizer_timeout", fired)


class TestLeadNodeFailure(unittest.TestCase):
    """The gap bcplus v2 round 8 fell through: the lead node died on a transport error,
    optimizer.js caught its null and returned a fallback object, so run.ts recorded a
    clean "ok" and no predicate fired. The fallback now says so via node_failed."""

    def test_dead_lead_fires_even_though_the_workflow_exited_ok(self):
        history = [rnd(1), rnd(2, result={"made_candidate": False, "node_failed": True})]
        self.assertIn("optimizer_lead_node_failed", compute_predicates(state(history)))

    def test_a_normal_result_object_does_not_fire(self):
        history = [rnd(1, result={"made_candidate": True, "summary": "shipped a candidate"})]
        self.assertEqual(compute_predicates(state(history)), [])

    def test_candidate_written_before_the_lead_died_still_counts_as_made(self):
        # write_workflow lands the file when called, so a lead that dies afterwards still
        # leaves a candidate behind. The predicate fires; the round is not discarded.
        history = [rnd(1, made=True, result={"node_failed": True})]
        fired = compute_predicates(state(history))
        self.assertEqual(fired, ["optimizer_lead_node_failed"])

    def test_non_dict_results_do_not_crash_the_predicate(self):
        # Records predating the fallback carry None; a text-returning workflow carries a str.
        for value in (None, "optimizer returned prose", [], 42):
            with self.subTest(result=value):
                self.assertEqual(compute_predicates(state([rnd(1, result=value)])), [])


class TestStreaks(unittest.TestCase):
    def test_no_candidate_streak_fires_at_the_threshold(self):
        history = [rnd(i, made=False, entered=False) for i in range(1, NO_CANDIDATE_STREAK + 1)]
        self.assertIn(f"no_candidate_{NO_CANDIDATE_STREAK}_rounds", compute_predicates(state(history)))

    def test_one_candidate_inside_the_window_breaks_the_streak(self):
        history = [rnd(i, made=False, entered=False) for i in range(1, NO_CANDIDATE_STREAK)]
        history.append(rnd(NO_CANDIDATE_STREAK, made=True, entered=False))
        self.assertNotIn(f"no_candidate_{NO_CANDIDATE_STREAK}_rounds", compute_predicates(state(history)))

    def test_stagnation_is_no_frontier_entry_not_a_flat_top_score(self):
        history = [rnd(i, entered=False) for i in range(1, STAGNATION_ROUNDS + 1)]
        self.assertIn(f"stagnation_{STAGNATION_ROUNDS}_rounds_no_frontier_entry", compute_predicates(state(history)))

    def test_a_cheaper_round_that_entered_the_frontier_is_progress(self):
        # Entering on the token axis alone clears stagnation — the whole point of the second axis.
        history = [rnd(i, entered=False) for i in range(1, STAGNATION_ROUNDS)]
        history.append(rnd(STAGNATION_ROUNDS, entered=True))
        self.assertEqual(compute_predicates(state(history)), [])


class TestStalledRounds(unittest.TestCase):
    """Trailing no-frontier-entry streak that gates the design-level hint in the round
    instruction (EXPLORE_HINT_ROUNDS). It counts from the tail: one entry resets it."""

    def test_empty_history_is_zero(self):
        self.assertEqual(stalled_rounds([]), 0)

    def test_counts_trailing_rounds_without_entry(self):
        history = [rnd(1), rnd(2, entered=False), rnd(3, entered=False)]
        self.assertEqual(stalled_rounds(history), 2)

    def test_a_frontier_entry_resets_the_streak(self):
        history = [rnd(1, entered=False), rnd(2, entered=False), rnd(3), rnd(4, entered=False)]
        self.assertEqual(stalled_rounds(history), 1)

    def test_no_candidate_rounds_count_as_stalled(self):
        history = [rnd(1), rnd(2, made=False, entered=False), rnd(3, made=False, entered=False)]
        self.assertEqual(stalled_rounds(history), 2)

    def test_hint_threshold_sits_below_the_watchdog_threshold(self):
        # The optimizer gets the evidence before the watchdog gets the case.
        self.assertLess(EXPLORE_HINT_ROUNDS, STAGNATION_ROUNDS)


if __name__ == "__main__":
    unittest.main()
