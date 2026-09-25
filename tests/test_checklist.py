import unittest

from inspection_catalog_core.checklist import (
    applicability_matches,
    half_open_overlaps,
    resolve_checklist,
)
from inspection_catalog_core.models import Applicability, ChecklistItem, EnterpriseOverride


def version(version_id="v1", template_id="t1", version_no=1, status="published",
            profiles=("p1",), tags=frozenset(), items=(), start="2026-01-01T00:00:00Z",
            end=None):
    from inspection_catalog_core.models import TemplateVersion
    return TemplateVersion(
        version_id=version_id, template_id=template_id, version_no=version_no, status=status,
        applicability=Applicability(frozenset(profiles), frozenset(tags)),
        items=tuple(ChecklistItem(**item) for item in items), content_hash="h",
        drafted_by="d", drafted_at="x", submitted_by=None, submitted_at=None,
        reviewed_by=None, reviewed_at=None, review_result=None, review_reason=None,
        published_by=None, published_at=None, effective_from=start, effective_to=end,
        revoked_by=None, revoked_at=None, revoke_reason=None)


def override(oid, site_id="s1", template_id=None, kind="add", code="E1",
             content=None, start="2026-01-01T00:00:00Z", end="2026-12-31T00:00:00Z",
             status="active"):
    return EnterpriseOverride(
        override_id=oid, site_id=site_id, template_id=template_id, kind=kind,
        item_code=code,
        content=ChecklistItem(**content) if content else None,
        reason="r", valid_from=start, valid_to=end, status=status,
        created_by="c", created_at="2026-01-01T00:00:00Z",
        revoked_by=None, revoked_at=None, revoke_reason=None)


class IntervalTest(unittest.TestCase):
    def test_adjacent_intervals_do_not_overlap(self):
        self.assertFalse(half_open_overlaps("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
                                            "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"))

    def test_overlapping_intervals_detected(self):
        self.assertTrue(half_open_overlaps("2026-01-01T00:00:00Z", None,
                                           "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"))
        self.assertTrue(half_open_overlaps("2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z",
                                           "2026-02-01T00:00:00Z", None))


class ApplicabilityTest(unittest.TestCase):
    def test_profile_intersection_required(self):
        app = Applicability(frozenset({"p1"}), frozenset())
        self.assertTrue(applicability_matches(app, ["p1", "p2"], []))
        self.assertFalse(applicability_matches(app, ["p2"], []))

    def test_tags_unrestricted_when_empty_but_required_when_declared(self):
        unrestricted = Applicability(frozenset({"p1"}), frozenset())
        self.assertTrue(applicability_matches(unrestricted, ["p1"], []))
        restricted = Applicability(frozenset({"p1"}), frozenset({"high_risk"}))
        self.assertTrue(applicability_matches(restricted, ["p1"], ["high_risk"]))
        self.assertFalse(applicability_matches(restricted, ["p1"], ["low_risk"]))


class ResolutionTest(unittest.TestCase):
    BASE_ITEMS = [
        {"code": "WG01", "category": "waste_gas", "content": "废气"},
        {"code": "DC01", "category": "dust_collection", "content": "粉尘"},
    ]

    def test_resolves_published_version_within_window(self):
        result = resolve_checklist(
            [version(items=self.BASE_ITEMS, start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z")],
            [], ["p1"], [], "2026-01-15T00:00:00Z")
        self.assertEqual(["DC01", "WG01"], [i.code for i in result["items"]])
        source = result["items"][0].sources[0]
        self.assertEqual("template_version", source["kind"])
        self.assertEqual("v1", source["version_id"])

    def test_excludes_version_outside_window_or_with_wrong_profile(self):
        v1 = version(items=self.BASE_ITEMS, start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z")
        result = resolve_checklist([v1], [], ["p1"], [], "2026-03-01T00:00:00Z")
        self.assertEqual((), result["items"])
        result = resolve_checklist([v1], [], ["p9"], [], "2026-01-15T00:00:00Z")
        self.assertEqual((), result["items"])

    def test_remove_override_deletes_item_and_leaves_trace(self):
        v1 = version(items=self.BASE_ITEMS)
        o1 = override("o1", kind="remove", code="DC01")
        result = resolve_checklist([v1], [o1], ["p1"], [], "2026-02-01T00:00:00Z")
        self.assertEqual(["WG01"], [i.code for i in result["items"]])
        self.assertEqual("DC01", result["removals"][0]["item_code"])
        self.assertEqual("o1", result["removals"][0]["override_id"])

    def test_add_override_appends_item_with_override_source(self):
        v1 = version(items=self.BASE_ITEMS)
        o1 = override("o1", kind="add", code="EX01",
                      content={"code": "EX01", "category": "other", "content": "补项"})
        result = resolve_checklist([v1], [o1], ["p1"], [], "2026-02-01T00:00:00Z")
        by_code = {i.code: i for i in result["items"]}
        self.assertEqual("补项", by_code["EX01"].content)
        self.assertEqual("enterprise_override", by_code["EX01"].sources[-1]["kind"])

    def test_override_scoped_to_other_template_is_ignored(self):
        v1 = version(template_id="t1", items=self.BASE_ITEMS)
        o1 = override("o1", template_id="t9", kind="remove", code="DC01")
        result = resolve_checklist([v1], [o1], ["p1"], [], "2026-02-01T00:00:00Z")
        self.assertEqual(["DC01", "WG01"], [i.code for i in result["items"]])

    def test_expired_or_revoked_override_ignored(self):
        v1 = version(items=self.BASE_ITEMS)
        o1 = override("o1", kind="remove", code="DC01",
                      start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z")
        result = resolve_checklist([v1], [o1], ["p1"], [], "2026-02-01T00:00:00Z")
        self.assertEqual(["DC01", "WG01"], [i.code for i in result["items"]])
        o2 = EnterpriseOverride(**{**o1.__dict__, "override_id": "o2", "status": "revoked",
                                   "valid_from": "2026-01-01T00:00:00Z",
                                   "valid_to": "2026-12-31T00:00:00Z"})
        result = resolve_checklist([v1], [o2], ["p1"], [], "2026-02-01T00:00:00Z")
        self.assertEqual(["DC01", "WG01"], [i.code for i in result["items"]])

    def test_multiple_templates_merge_by_code_and_stable_order(self):
        v1 = version(version_id="v1", template_id="ta", version_no=1, items=self.BASE_ITEMS)
        v2 = version(version_id="v2", template_id="tb", version_no=1, items=[
            {"code": "HW01", "category": "hazardous_waste", "content": "危废"},
            {"code": "WG01", "category": "waste_gas", "content": "废气新版"},
        ])
        result = resolve_checklist([v1, v2], [], ["p1"], [], "2026-02-01T00:00:00Z")
        by_code = {i.code: i for i in result["items"]}
        # 同编码以排序靠后的模板 tb 为准，但两个版本都保留在来源链上。
        self.assertEqual("废气新版", by_code["WG01"].content)
        self.assertEqual(["ta", "tb"], [s["template_id"] for s in by_code["WG01"].sources])
        self.assertEqual(["DC01", "HW01", "WG01"], [i.code for i in result["items"]])


if __name__ == "__main__":
    unittest.main()
