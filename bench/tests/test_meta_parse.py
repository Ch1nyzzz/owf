"""Hardened parsing of meta-agent dispatch replies.

Run: PYTHONPATH=bench python3 -m unittest discover -s bench/tests
"""

import unittest

from owf_bench.core.meta_delegate import parse_decision

PRESETS = {"seed": "seed.js", "iter_002": "i2.js"}


class TestParseDecision(unittest.TestCase):
    def test_plain_json(self):
        d = parse_decision('{"preset": "seed", "reason": "r"}', PRESETS, False)
        self.assertEqual(d["preset"], "seed")

    def test_fenced_block_with_prose_around(self):
        text = 'Reasoning here.\n```json\n{"preset": "iter_002", "reason": "match"}\n```\nDone.'
        self.assertEqual(parse_decision(text, PRESETS, False)["preset"], "iter_002")

    def test_last_of_multiple_objects_wins(self):
        text = 'consider {"preset": "seed"} ... final answer: {"preset": "iter_002", "reason": "x"}'
        self.assertEqual(parse_decision(text, PRESETS, False)["preset"], "iter_002")

    def test_case_and_prefix_normalised(self):
        self.assertEqual(parse_decision('{"preset": "Seed"}', PRESETS, False)["preset"], "seed")
        self.assertEqual(parse_decision('{"preset": "preset:iter_002"}', PRESETS, False)["preset"], "iter_002")

    def test_unknown_preset_still_rejected(self):
        with self.assertRaises(ValueError):
            parse_decision('{"preset": "iter_999"}', PRESETS, False)

    def test_empty_response_named_clearly(self):
        with self.assertRaises(ValueError) as ctx:
            parse_decision("  ", PRESETS, False)
        self.assertIn("empty meta response", str(ctx.exception))

    def test_assembly_rejected_when_novel_disabled(self):
        with self.assertRaises(ValueError) as ctx:
            parse_decision('{"assembly": {"prompt": "evidence_lead"}}', PRESETS, False)
        self.assertIn("disabled", str(ctx.exception))

    def test_nested_braces_in_reason_survive(self):
        d = parse_decision('{"preset": "seed", "reason": "matches {rare} pattern"}', PRESETS, False)
        self.assertEqual(d["preset"], "seed")


if __name__ == "__main__":
    unittest.main()
