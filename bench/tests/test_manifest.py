"""Declaration protocol: manifest validation and inventory bookkeeping.

Run: PYTHONPATH=bench python3 -m unittest discover -s bench/tests
"""

import unittest

from owf_bench.core.optimize import SLOT_VOCAB_V1, apply_manifest, validate_manifest


def inventory():
    return {
        "slots": dict(SLOT_VOCAB_V1),
        "components": {"prompt.seed_persistent": {"slot": "research_prompt", "desc": "seed"}},
        "members": {"seed": {"prompt.seed_persistent": 1}},
        "params": {},
    }


def manifest(action="NEW_COMPONENT", components=None, **kw):
    return {"action": action,
            "components": components if components is not None
            else {"closure.post_editor": {"slot": "closure", "desc": "post-hoc editor"}}, **kw}


class TestValidate(unittest.TestCase):
    def test_valid_new_component(self):
        self.assertEqual(validate_manifest(manifest(), inventory()), [])

    def test_missing_manifest_is_one_clear_error(self):
        self.assertEqual(validate_manifest(None, inventory()), ["manifest missing or not an object"])

    def test_unknown_slot_rejected(self):
        m = manifest(components={"widget.x": {"slot": "widget", "desc": "?"}})
        self.assertTrue(any("unknown or missing slot" in e for e in validate_manifest(m, inventory())))

    def test_new_assembly_may_not_introduce_components(self):
        m = manifest(action="NEW_ASSEMBLY")
        self.assertTrue(any("may not introduce" in e for e in validate_manifest(m, inventory())))

    def test_new_assembly_of_existing_ids_passes(self):
        m = manifest(action="NEW_ASSEMBLY",
                     components={"prompt.seed_persistent": {"slot": "research_prompt", "params": {"turn_budget": 48}}})
        self.assertEqual(validate_manifest(m, inventory()), [])

    def test_new_slot_requires_new_slots_and_accepts_its_components(self):
        m = manifest(action="NEW_SLOT")
        self.assertTrue(any("requires new_slots" in e for e in validate_manifest(m, inventory())))
        m = manifest(action="NEW_SLOT", new_slots={"memory": "cross-node state"},
                     components={"memory.ledger": {"slot": "memory", "desc": "candidate ledger"}})
        self.assertEqual(validate_manifest(m, inventory()), [])

    def test_new_component_must_actually_be_new(self):
        m = manifest(components={"prompt.seed_persistent": {"slot": "research_prompt", "desc": "seed"}})
        self.assertTrue(any("already exists" in e for e in validate_manifest(m, inventory())))

    def test_bad_id_format_rejected(self):
        m = manifest(components={"NoDots": {"slot": "closure", "desc": "x"}})
        self.assertTrue(any("bad component id" in e for e in validate_manifest(m, inventory())))


class TestApply(unittest.TestCase):
    def test_apply_grows_inventory_without_rewriting(self):
        inv = inventory()
        m = manifest(components={
            "prompt.seed_persistent": {"slot": "research_prompt", "desc": "SHOULD NOT OVERWRITE"},
            "closure.post_editor": {"slot": "closure", "desc": "editor", "params": {"window": 8000}},
        })
        apply_manifest(inv, "iter_001", m)
        self.assertEqual(inv["components"]["prompt.seed_persistent"]["desc"], "seed")  # merge, no overwrite
        self.assertEqual(inv["components"]["closure.post_editor"]["slot"], "closure")
        self.assertEqual(inv["members"]["iter_001"],
                         {"prompt.seed_persistent": 1, "closure.post_editor": 1})
        self.assertEqual(inv["params"]["iter_001"]["closure.post_editor"], {"window": 8000})
        self.assertEqual(inv["manifest_log"][-1], {"member": "iter_001", "action": "NEW_COMPONENT"})

    def test_new_slot_lands_in_vocabulary(self):
        inv = inventory()
        m = manifest(action="NEW_SLOT", new_slots={"memory": "cross-node state"},
                     components={"memory.ledger": {"slot": "memory", "desc": "ledger"}})
        apply_manifest(inv, "iter_002", m)
        self.assertIn("memory", inv["slots"])


if __name__ == "__main__":
    unittest.main()
