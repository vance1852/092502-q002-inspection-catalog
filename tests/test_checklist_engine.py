import unittest

from inspection_catalog_core.checklist_engine import (
    change_from_dict,
    item_applies,
    item_from_dict,
    resolve_checklist,
)
from inspection_catalog_core.checklist_models import ChecklistItem


def item(code, processes=None, risks=None, content=None):
    return ChecklistItem(code, content or f"条目{code}", "cat",
                         frozenset(processes or ()), frozenset(risks or ()), "")


class ApplicabilityTest(unittest.TestCase):
    def test_unconditional_item_always_applies(self):
        self.assertTrue(item_applies(item("G"), frozenset(), frozenset()))

    def test_process_requires_intersection(self):
        it = item("G", processes=["spray"])
        self.assertFalse(item_applies(it, frozenset(["cutting"]), frozenset()))
        self.assertTrue(item_applies(it, frozenset(["spray"]), frozenset()))

    def test_both_process_and_risk_must_match(self):
        it = item("G", processes=["spray"], risks=["gas"])
        self.assertFalse(item_applies(it, frozenset(["spray"]), frozenset()))
        self.assertTrue(item_applies(it, frozenset(["spray"]), frozenset(["gas"])))


class OverlayResolutionTest(unittest.TestCase):
    BASE = (item("G1", processes=["spray"]), item("G2"), item("G3"))

    def resolve(self, processes, risks, overlays):
        codes = resolve_checklist(version_id="v1", items=self.BASE,
                                  processes=processes, risks=risks, overlays=overlays)
        return {r.code: r for r in codes}

    def test_base_filtering_and_order_preserved(self):
        result = self.resolve(["spray"], [], [])
        self.assertEqual(["G1", "G2", "G3"], list(result))

    def test_overlay_remove(self):
        change = change_from_dict({"kind": "remove", "code": "G2"})
        result = self.resolve([], [], [("o1", 1, [change])])
        self.assertNotIn("G2", result)

    def test_overlay_modify_records_source_chain(self):
        change = change_from_dict({"kind": "modify", "code": "G2", "content": "改后"})
        result = self.resolve([], [], [("o1", 1, [change])])
        self.assertEqual("改后", result["G2"].content)
        layers = [s["layer"] for s in result["G2"].sources]
        self.assertEqual(["base", "overlay_modify"], layers)

    def test_overlay_add_appended(self):
        add = change_from_dict({"kind": "add", "code": "X1",
                                "item": {"code": "X1", "content": "新增", "category": "cat"}})
        result = self.resolve([], [], [("o1", 1, [add])])
        self.assertEqual(["G2", "G3", "X1"], list(result))
        self.assertEqual("overlay_add", result["X1"].sources[0]["layer"])

    def test_later_overlay_wins_in_deterministic_order(self):
        first = change_from_dict({"kind": "modify", "code": "G2", "content": "第一版"})
        second = change_from_dict({"kind": "modify", "code": "G2", "content": "第二版"})
        result = self.resolve([], [], [("o1", 1, [first]), ("o2", 2, [second])])
        self.assertEqual("第二版", result["G2"].content)
        self.assertEqual(3, len(result["G2"].sources))

    def test_remove_then_readd_restores_as_overlay_item(self):
        remove = change_from_dict({"kind": "remove", "code": "G2"})
        readd = change_from_dict({"kind": "add", "code": "G2",
                                  "item": {"code": "G2", "content": "复活", "category": "cat"}})
        result = self.resolve([], [], [("o1", 1, [remove]), ("o2", 2, [readd])])
        self.assertEqual("复活", result["G2"].content)
        self.assertEqual("overlay_add", result["G2"].sources[0]["layer"])

    def test_modify_missing_target_is_ignored(self):
        change = change_from_dict({"kind": "modify", "code": "G1", "content": "x"})
        # G1 需要 spray 工艺，企业没有该工艺，modify 不应凭空生效。
        result = self.resolve([], [], [("o1", 1, [change])])
        self.assertNotIn("G1", result)

    def test_item_from_dict_validates(self):
        with self.assertRaises(ValueError):
            item_from_dict({"code": "", "content": "x", "category": "c"})
        with self.assertRaises(ValueError):
            change_from_dict({"kind": "delete", "code": "G1"})


if __name__ == "__main__":
    unittest.main()
