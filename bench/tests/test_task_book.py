"""Per-task champion book: ownership, confirmation, coverage, deltas.

Run: PYTHONPATH=bench python3 -m unittest discover -s bench/tests
"""

import json
import tempfile
import unittest
from pathlib import Path

from owf_bench.core.task_book import (
    beats,
    build_book,
    confirm_targets,
    diff_books,
    emit_confirm_commands,
    make_entry,
    next_confirm_round,
)


def sample(score, tokens):
    return {"score": score, "tokens": tokens, "rollout": "x"}


def entry(member, samples):
    return make_entry(member, f"{member}.js", samples)


def write_source(source_dir: Path, workflow: str, rows: dict[str, list[tuple[float, int]]],
                 match_type: str = "judge") -> None:
    """rows: task_id -> [(score, tokens), ...] one tuple per rep."""
    source_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for tid, reps in rows.items():
        for rep, (score, tokens) in enumerate(reps):
            lines.append(json.dumps({
                "task_id": tid, "rep": rep, "status": "ok", "score": score,
                "match_type": match_type,
                "tokens": {"input": tokens, "output": 0},
            }))
    (source_dir / "results.jsonl").write_text("\n".join(lines) + "\n")
    scores = {tid: sum(s for s, _ in reps) / len(reps) for tid, reps in rows.items()}
    (source_dir / "report.json").write_text(json.dumps({"workflow": workflow, "task_scores": scores}))


class TestEntryStatus(unittest.TestCase):
    def test_single_rep_pass_is_provisional(self):
        self.assertEqual(entry("a", [sample(1.0, 100)])["status"], "provisional")

    def test_two_of_three_reps_is_confirmed(self):
        e = entry("a", [sample(1.0, 100), sample(1.0, 120), sample(0.0, 90)])
        self.assertEqual(e["status"], "confirmed")

    def test_one_of_three_reps_is_provisional(self):
        e = entry("a", [sample(1.0, 100), sample(0.0, 120), sample(0.0, 90)])
        self.assertEqual(e["status"], "provisional")

    def test_no_pass_is_fail(self):
        self.assertEqual(entry("a", [sample(0.0, 100)])["status"], "fail")

    def test_mean_tokens_covers_all_reps_including_failures(self):
        e = entry("a", [sample(1.0, 100), sample(0.0, 300)])
        self.assertEqual(e["mean_tokens"], 200)


class TestChallenge(unittest.TestCase):
    def test_confirmed_beats_provisional_even_when_pricier(self):
        c = entry("c", [sample(1.0, 500), sample(1.0, 500)])
        p = entry("p", [sample(1.0, 100)])
        won, why = beats(c, p, 0.10)
        self.assertTrue(won)
        self.assertEqual(why, "stronger")
        self.assertFalse(beats(p, c, 0.10)[0])

    def test_same_strength_needs_token_margin(self):
        owner = entry("o", [sample(1.0, 100)])
        within = entry("w", [sample(1.0, 95)])   # 5% cheaper: inside the 10% margin
        beyond = entry("b", [sample(1.0, 85)])   # 15% cheaper: takes the task
        self.assertFalse(beats(within, owner, 0.10)[0])
        won, why = beats(beyond, owner, 0.10)
        self.assertTrue(won)
        self.assertEqual(why, "cheaper")

    def test_higher_pass_rate_wins_at_same_status(self):
        # both provisional (1-rep vs 3-rep 1/3): pass_rate decides
        strong = entry("s", [sample(1.0, 500)])
        weak = entry("w", [sample(1.0, 100), sample(0.0, 100), sample(0.0, 100)])
        self.assertTrue(beats(strong, weak, 0.10)[0])


class TestBuildBook(unittest.TestCase):
    def make_root(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "state.json").write_text(json.dumps({"domain": "toy"}))
        # seed: solves t1 (k=2 confirmed), fails t2, never saw t3
        write_source(root / "evidence/baseline", "seed.js",
                     {"t1": [(1.0, 100), (1.0, 110)], "t2": [(0.0, 200)]})
        # iter_001: solves t2 (claims), solves t1 at k=1 (provisional loses to confirmed)
        write_source(root / "iter_001/eval", "i1.js",
                     {"t1": [(1.0, 50)], "t2": [(1.0, 300)], "t3": [(0.0, 10)]})
        # iter_002: retakes t2 cheaper (>10% under 300)
        write_source(root / "iter_002/eval", "i2.js", {"t2": [(1.0, 150)]})
        return root

    def test_ownership_and_coverage(self):
        book = build_book(self.make_root())
        self.assertEqual(book["tasks"]["t1"]["owner"], "seed")  # confirmed holds off cheap k=1
        self.assertEqual(book["tasks"]["t2"]["owner"], "iter_002")  # cheaper beyond margin
        cov = book["coverage"]
        self.assertEqual((cov["universe"], cov["union"], cov["confirmed"]), (3, 2, 1))
        self.assertEqual(cov["unsolved"], ["t3"])
        self.assertEqual(cov["provisional_only"], ["t2"])

    def test_ownership_log_records_takeover(self):
        book = build_book(self.make_book_root_with_log())
        log = book["tasks"]["t2"]["log"]
        self.assertIn("iter_001 claimed", log[0])
        self.assertIn("iter_002 took over from iter_001 (cheaper", log[1])

    make_book_root_with_log = make_root

    def test_confirm_targets_group_by_owner(self):
        book = build_book(self.make_root())
        self.assertEqual(confirm_targets(book), {"iter_002": ["t2"]})

    def test_confirmation_run_flips_status_and_diff_reports_it(self):
        root = self.make_root()
        before = build_book(root)
        # targeted confirm run for iter_002 on t2: 2 more passing reps
        write_source(root / "confirm/iter_002", "i2.js", {"t2": [(1.0, 140), (1.0, 160)]})
        after = build_book(root)
        self.assertEqual(after["tasks"]["t2"]["entries"]["iter_002"]["status"], "confirmed")
        self.assertEqual(after["coverage"]["confirmed"], 2)
        self.assertEqual(after["coverage"]["provisional_only"], [])
        delta = diff_books(before, after)
        self.assertEqual(delta["claimed"], [])
        self.assertEqual(delta["union"], "2 -> 2")

    def test_diff_reports_claims_and_takeovers(self):
        root = self.make_root()
        partial = build_book(root)
        write_source(root / "iter_003/eval", "i3.js", {"t3": [(1.0, 80)], "t2": [(1.0, 40)]})
        full = build_book(root)
        delta = diff_books(partial, full)
        self.assertEqual(delta["claimed"], ["t3"])
        self.assertEqual([d["task"] for d in delta["took_over"]], ["t2"])
        self.assertEqual(delta["union"], "2 -> 3")

    def test_cover_set_covers_the_union(self):
        book = build_book(self.make_root())
        covered = sum(c["marginal"] for c in book["cover_set"])
        self.assertEqual(covered, book["coverage"]["union"])

    def test_rebuild_is_idempotent(self):
        root = self.make_root()
        self.assertEqual(build_book(root), build_book(root))

    def test_judge_error_rows_are_not_evidence(self):
        # A dead judge scores everything 0 with match_type judge_error; those reps
        # must neither create entries nor drag down an existing owner's pass rate.
        root = self.make_root()
        before = build_book(root)
        write_source(root / "confirm/seed", "seed.js",
                     {"t1": [(0.0, 100), (0.0, 100)]}, match_type="judge_error")
        after = build_book(root)
        self.assertEqual(after["tasks"]["t1"]["entries"]["seed"],
                         before["tasks"]["t1"]["entries"]["seed"])
        self.assertEqual(after["tasks"]["t1"]["owner"], "seed")

    def test_round_dirs_accumulate_as_sources(self):
        # Evidence from confirm/<m> (round 1) and confirm/round_002/<m> merges;
        # separate dirs per round is what stops runner from rewriting earlier rows.
        root = self.make_root()
        write_source(root / "confirm/iter_002", "i2.js", {"t2": [(1.0, 140), (0.0, 160)]})
        write_source(root / "confirm/round_002/iter_002", "i2.js", {"t2": [(1.0, 150)]})
        book = build_book(root)
        e = book["tasks"]["t2"]["entries"]["iter_002"]
        self.assertEqual((e["reps"], e["passes"]), (4, 3))  # 1 eval + 2 r1 + 1 r2
        self.assertEqual(e["status"], "confirmed")

    def test_emitted_commands_target_a_fresh_round_dir(self):
        root = self.make_root()
        write_source(root / "confirm/round_002/iter_002", "i2.js", {"t9": [(1.0, 1)]})
        self.assertEqual(next_confirm_round(root).name, "round_003")
        book = build_book(root)
        for cmd in emit_confirm_commands(book, 2, 1000, 60):
            self.assertIn("confirm/round_003/", cmd)

    def test_member_with_only_judge_error_rows_has_no_entry(self):
        root = self.make_root()
        write_source(root / "iter_004/eval", "i4.js",
                     {"t3": [(0.0, 50)]}, match_type="judge_error")
        book = build_book(root)
        self.assertNotIn("iter_004", book["tasks"].get("t3", {}).get("entries", {}))
        self.assertIn("t3", book["coverage"]["unsolved"])

    def test_imports_fold_last_and_carry_foreign_evidence(self):
        # An external member from lab/imports.json merges its declared evidence
        # verbatim; folding last, it must strictly beat a local incumbent to own.
        root = self.make_root()
        foreign = Path(tempfile.mkdtemp())
        write_source(foreign / "eval", "ext.js",
                     {"t1": [(1.0, 100), (1.0, 100)],   # confirmed but not cheaper: no takeover
                      "t3": [(1.0, 80)]})               # claims the locally unsolved task
        (root / "lab").mkdir()
        (root / "lab/imports.json").write_text(json.dumps(
            {"ext_a": {"workflow": "ext.js", "sources": [str(foreign / "eval")]}}))
        book = build_book(root)
        self.assertEqual(book["member_order"][-1], "ext_a")
        self.assertEqual(book["tasks"]["t1"]["owner"], "seed")
        self.assertEqual(book["tasks"]["t3"]["owner"], "ext_a")
        self.assertEqual(book["coverage"]["union"], 3)

    def test_import_with_missing_evidence_raises(self):
        root = self.make_root()
        (root / "lab").mkdir()
        (root / "lab/imports.json").write_text(json.dumps(
            {"ext_a": {"workflow": "ext.js", "sources": [str(root / "nowhere")]}}))
        with self.assertRaises(FileNotFoundError):
            build_book(root)


if __name__ == "__main__":
    unittest.main()
