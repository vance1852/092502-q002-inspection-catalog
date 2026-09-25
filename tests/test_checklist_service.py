import json
import unittest
from datetime import datetime, timezone

from inspection_catalog_core.checklist_service import ChecklistService
from inspection_catalog_core.clock import FixedClock
from inspection_catalog_core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from inspection_catalog_core.service import DomainService
from inspection_catalog_core.storage import Database


ITEMS = [
    {"code": "WG01", "category": "waste_gas", "content": "废气治理设施运行"},
    {"code": "DC01", "category": "dust_collection", "content": "粉尘收集装置"},
    {"code": "HW01", "category": "hazardous_waste", "content": "危废贮存台账"},
]


class ChecklistServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, self.clock)
        self.service = ChecklistService(self.database, self.clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="街道")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                   display_name="业务员", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                   display_name="复核员", role="reviewer", organization_id="o1")
        self.domain.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                   display_name="审计员", role="auditor", organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="家具厂", timezone_name="Asia/Shanghai")
        self.domain.record_domain_data(request_id="pp", actor_id="op1", site_id="s1",
                                       category="process_profile", external_key="spray_paint",
                                       data={"name": "喷涂"})
        self.domain.record_domain_data(request_id="tag", actor_id="op1", site_id="s1",
                                       category="enterprise_tag", external_key="high_risk",
                                       data={"name": "高风险"})

    def tearDown(self):
        self.database.close()

    def draft_published(self, *, request_prefix="x", from_="2026-09-01", to_=None,
                        profiles=("spray_paint",), tags=("high_risk",), items=None,
                        template_id=None):
        """起草、复核、发布一个版本并返回 (template_id, version_id)。"""

        if template_id is None:
            template_id = self.service.create_template(
                request_id=f"{request_prefix}-t", actor_id="op1", name="清单").resource_id
        version_id = self.service.create_template_version(
            request_id=f"{request_prefix}-v", actor_id="op1", template_id=template_id,
            applicability={"process_profiles": list(profiles), "enterprise_tags": list(tags)},
            items=items or ITEMS).resource_id
        self.service.submit_version(request_id=f"{request_prefix}-s", actor_id="op1",
                                    version_id=version_id)
        self.service.review_version(request_id=f"{request_prefix}-r", actor_id="rv1",
                                    version_id=version_id, result="approved")
        self.service.publish_version(request_id=f"{request_prefix}-p", actor_id="rv1",
                                     version_id=version_id, effective_from=from_,
                                     effective_to=to_)
        return template_id, version_id

    # ------------------------------------------------------------------
    # 生命周期与四眼复核
    # ------------------------------------------------------------------

    def test_version_must_pass_review_before_publish(self):
        _, version_id = self.draft_template()
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="pp", actor_id="rv1",
                                         version_id=version_id, effective_from="2026-09-01")

    def draft_template(self):
        template_id = self.service.create_template(
            request_id="tt", actor_id="op1", name="清单").resource_id
        version_id = self.service.create_template_version(
            request_id="vv", actor_id="op1", template_id=template_id,
            applicability={"process_profiles": ["spray_paint"], "enterprise_tags": ["high_risk"]},
            items=ITEMS).resource_id
        return template_id, version_id

    def test_submitter_cannot_review_own_version(self):
        _, version_id = self.draft_template()
        self.service.submit_version(request_id="ss", actor_id="op1", version_id=version_id)
        with self.assertRaises(PermissionDenied):
            self.service.review_version(request_id="rr", actor_id="op1",
                                        version_id=version_id, result="approved")
        # admin 也不能复核自己提交的版本
        self.service.submit_version  # noqa: B018 - 保持可读性
        admin_version = self.service.create_template_version(
            request_id="vva", actor_id="a1", template_id=self._template_of(version_id),
            applicability={"process_profiles": ["spray_paint"], "enterprise_tags": []},
            items=ITEMS).resource_id
        self.service.submit_version(request_id="ssa", actor_id="a1", version_id=admin_version)
        with self.assertRaises(PermissionDenied):
            self.service.review_version(request_id="rra", actor_id="a1",
                                        version_id=admin_version, result="approved")

    def _template_of(self, version_id):
        return self.service.get_version(version_id).template_id

    def test_rejected_version_cannot_publish_but_redraft_can(self):
        _, version_id = self.draft_template()
        self.service.submit_version(request_id="ss", actor_id="op1", version_id=version_id)
        self.service.review_version(request_id="rr", actor_id="rv1", version_id=version_id,
                                    result="rejected", reason="条目缺失")
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="pp", actor_id="rv1",
                                         version_id=version_id, effective_from="2026-09-01")
        # 被驳回的版本不能直接修改，需要起草新版本
        with self.assertRaises(ConflictError):
            self.service.update_template_version(
                request_id="uu", actor_id="op1", version_id=version_id,
                applicability={"process_profiles": ["spray_paint"], "enterprise_tags": []},
                items=ITEMS)

    def test_only_draft_can_be_edited(self):
        template_id, version_id = self.draft_template()
        self.service.update_template_version(
            request_id="uu", actor_id="op1", version_id=version_id,
            applicability={"process_profiles": ["spray_paint"], "enterprise_tags": []},
            items=ITEMS[:2])
        self.service.submit_version(request_id="ss", actor_id="op1", version_id=version_id)
        with self.assertRaises(ConflictError):
            self.service.update_template_version(
                request_id="uu2", actor_id="op1", version_id=version_id,
                applicability={"process_profiles": ["spray_paint"], "enterprise_tags": []},
                items=ITEMS[:2])

    def test_duplicate_publish_is_conflict_but_idempotent_replay(self):
        _, version_id = self.draft_published()
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="p2", actor_id="rv1",
                                         version_id=version_id, effective_from="2026-09-01")
        replay = self.service.publish_version(request_id="x-p", actor_id="rv1",
                                              version_id=version_id, effective_from="2026-09-01")
        self.assertTrue(replay.replayed)

    def test_overlapping_effective_windows_rejected_adjacent_allowed(self):
        template_id, v1 = self.draft_published(request_prefix="a",
                                               from_="2026-09-01", to_="2026-10-01")
        # 同模板下一版本区间重叠 -> 冲突
        _, v2 = self._draft_approved(template_id, "b", from_="2026-09-15", to_="2026-11-01")
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="b-p", actor_id="rv1", version_id=v2,
                                         effective_from="2026-09-15", effective_to="2026-11-01")
        # 相邻（半开端点相接）允许
        _, v3 = self._draft_approved(template_id, "c", from_="2026-10-01", to_="2026-11-01")
        self.service.publish_version(request_id="c-p", actor_id="rv1", version_id=v3,
                                     effective_from="2026-10-01", effective_to="2026-11-01")

    def _draft_approved(self, template_id, prefix, *, from_, to_, tags=("high_risk",)):
        version_id = self.service.create_template_version(
            request_id=f"{prefix}-v", actor_id="op1", template_id=template_id,
            applicability={"process_profiles": ["spray_paint"], "enterprise_tags": list(tags)},
            items=ITEMS).resource_id
        self.service.submit_version(request_id=f"{prefix}-s", actor_id="op1", version_id=version_id)
        self.service.review_version(request_id=f"{prefix}-r", actor_id="rv1",
                                    version_id=version_id, result="approved")
        return template_id, version_id

    def test_invalid_effective_window_rejected(self):
        _, version_id = self.draft_template()
        self.service.submit_version(request_id="ss", actor_id="op1", version_id=version_id)
        self.service.review_version(request_id="rr", actor_id="rv1", version_id=version_id,
                                    result="approved")
        with self.assertRaises(ValidationError):
            self.service.publish_version(request_id="pp", actor_id="rv1", version_id=version_id,
                                         effective_from="2026-10-01", effective_to="2026-09-01")

    # ------------------------------------------------------------------
    # 企业覆盖层
    # ------------------------------------------------------------------

    def test_override_requires_reason_and_bounded_duration(self):
        with self.assertRaises(ValidationError):
            self.service.add_override(request_id="oo0", actor_id="op1", site_id="s1", kind="add",
                                      item_code="EX01", reason="  ",
                                      valid_from="2026-09-01", valid_to="2026-09-10",
                                      content={"category": "other", "content": "x"})
        with self.assertRaises(ValidationError):
            self.service.add_override(request_id="oo1", actor_id="op1", site_id="s1", kind="add",
                                      item_code="EX01", reason="合理理由",
                                      valid_from="2026-09-01", valid_to="2027-09-03",
                                      content={"category": "other", "content": "x"})

    def test_add_override_requires_content_remove_forbids_it(self):
        with self.assertRaises(ValidationError):
            self.service.add_override(request_id="oo1", actor_id="op1", site_id="s1", kind="add",
                                      item_code="EX01", reason="r",
                                      valid_from="2026-09-01", valid_to="2026-09-10")
        with self.assertRaises(ValidationError):
            self.service.add_override(request_id="oo2", actor_id="op1", site_id="s1", kind="remove",
                                      item_code="WG01", reason="r",
                                      valid_from="2026-09-01", valid_to="2026-09-10",
                                      content={"category": "waste_gas", "content": "x"})

    def test_overlapping_override_windows_rejected(self):
        kwargs = dict(actor_id="op1", site_id="s1", kind="add", item_code="EX01",
                      content={"category": "other", "content": "x"})
        self.service.add_override(request_id="oo1", reason="r1",
                                  valid_from="2026-09-01", valid_to="2026-09-10", **kwargs)
        with self.assertRaises(ConflictError):
            self.service.add_override(request_id="oo2", reason="r2",
                                      valid_from="2026-09-09", valid_to="2026-09-20", **kwargs)
        # 不同 item_code 之间允许重叠
        self.service.add_override(request_id="oo3", reason="r3", item_code="EX02",
                                  valid_from="2026-09-01", valid_to="2026-09-20",
                                  actor_id="op1", site_id="s1", kind="add",
                                  content={"category": "other", "content": "y"})

    # ------------------------------------------------------------------
    # 任务冻结
    # ------------------------------------------------------------------

    def test_generated_task_freezes_items_and_sources(self):
        _, version_id = self.draft_published()
        self.service.add_override(request_id="ooa", actor_id="op1", site_id="s1", kind="add",
                                  item_code="EX01", reason="信访", valid_from="2026-09-01",
                                  valid_to="2026-09-30",
                                  content={"category": "other", "content": "补项"})
        receipt = self.service.generate_daily_task(
            request_id="gg", actor_id="op1", site_id="s1", task_date="2026-09-10")
        task = self.service.get_task(receipt.resource_id)
        codes = [item.code for item in task.items]
        self.assertEqual(["DC01", "EX01", "HW01", "WG01"], codes)
        self.assertEqual((version_id,), task.version_ids)
        self.assertTrue(all(item.sources for item in task.items))
        self.assertTrue(task.snapshot_hash)

    def test_started_task_not_rewritten_by_later_publish_revoke_or_tag_change(self):
        _, version_id = self.draft_published(from_="2026-09-01", to_="2026-10-01")
        receipt = self.service.generate_daily_task(
            request_id="gg", actor_id="op1", site_id="s1", task_date="2026-09-10")
        task_before = self.service.get_task(receipt.resource_id)
        self.service.start_task(actor_id="op1", task_id=receipt.resource_id)

        # 后来发布新版本、撤销旧版本、标签变化
        self.service.revoke_version(request_id="rk", actor_id="rv1", version_id=version_id,
                                    reason="废止")
        self.database.connection.execute(
            "UPDATE domain_records SET payload_json=? WHERE external_key='high_risk'",
            (json.dumps({"name": "高风险", "enabled": False}, ensure_ascii=False,
                        sort_keys=True),))

        current = self.service.current_checklist("s1", "2026-09-10T00:00:00Z")
        self.assertEqual([], current["matched_versions"])
        task_after = self.service.get_task(receipt.resource_id)
        self.assertEqual([i.code for i in task_before.items],
                         [i.code for i in task_after.items])
        self.assertEqual(task_before.snapshot_hash, task_after.snapshot_hash)
        self.assertEqual("started", task_after.status)

    def test_revoke_only_affects_tasks_generated_afterwards(self):
        _, version_id = self.draft_published(from_="2026-09-01", to_="2026-12-31")
        first = self.service.generate_daily_task(
            request_id="g1", actor_id="op1", site_id="s1", task_date="2026-09-10")
        self.assertEqual(3, len(self.service.get_task(first.resource_id).items))
        self.service.revoke_version(request_id="rk", actor_id="rv1", version_id=version_id,
                                    reason="废止")
        second = self.service.generate_daily_task(
            request_id="g2", actor_id="op1", site_id="s1", task_date="2026-09-11")
        self.assertEqual(0, len(self.service.get_task(second.resource_id).items))

    def test_same_day_generation_is_idempotent_and_does_not_resolve_again(self):
        self.draft_published(from_="2026-09-01", to_="2026-12-31")
        first = self.service.generate_daily_task(
            request_id="g1", actor_id="op1", site_id="s1", task_date="2026-09-10")
        second = self.service.generate_daily_task(
            request_id="different-request", actor_id="op1", site_id="s1", task_date="2026-09-10")
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        # 即使在撤销版本后重放，仍返回原任务
        self.service.revoke_version(request_id="rk", actor_id="rv1",
                                    version_id=self.service.get_task(first.resource_id).version_ids[0],
                                    reason="废止")
        third = self.service.generate_daily_task(
            request_id="g3", actor_id="op1", site_id="s1", task_date="2026-09-10")
        self.assertEqual(first.resource_id, third.resource_id)
        self.assertEqual(3, len(self.service.get_task(third.resource_id).items))

    def test_task_lifecycle_transitions_are_guarded(self):
        self.draft_published()
        receipt = self.service.generate_daily_task(
            request_id="gg", actor_id="op1", site_id="s1", task_date="2026-09-10")
        # 未开始不能完成
        with self.assertRaises(ConflictError):
            self.service.complete_task(actor_id="op1", task_id=receipt.resource_id)
        self.service.start_task(actor_id="op1", task_id=receipt.resource_id)
        # 已开始不能再次开始
        with self.assertRaises(ConflictError):
            self.service.start_task(actor_id="op1", task_id=receipt.resource_id)
        result = self.service.complete_task(actor_id="op1", task_id=receipt.resource_id)
        self.assertEqual("completed", result["status"])
        with self.assertRaises(ConflictError):
            self.service.cancel_task(request_id="cc", actor_id="op1",
                                     task_id=receipt.resource_id, reason="r")

    # ------------------------------------------------------------------
    # 查询：当前清单、未来变更、历史溯源
    # ------------------------------------------------------------------

    def test_current_checklist_respects_applicability_and_tags(self):
        self.draft_published(tags=("high_risk",), from_="2026-09-01", to_="2026-12-31")
        matched = self.service.current_checklist("s1", "2026-09-10T00:00:00Z")
        self.assertEqual(3, len(matched["items"]))
        # 标签变化后不再适用
        self.database.connection.execute(
            "UPDATE domain_records SET payload_json=? WHERE external_key='high_risk'",
            (json.dumps({"name": "高风险", "enabled": False}, ensure_ascii=False,
                        sort_keys=True),))
        self.assertEqual(0, len(self.service.current_checklist("s1", "2026-09-10T00:00:00Z")["items"]))

    def test_future_changes_reports_version_and_override_events(self):
        self.draft_published(from_="2026-09-10", to_="2026-10-10")
        self.service.add_override(request_id="ooa", actor_id="op1", site_id="s1", kind="add",
                                  item_code="EX01", reason="r",
                                  valid_from="2026-09-20", valid_to="2026-09-25",
                                  content={"category": "other", "content": "x"})
        changes = self.service.future_changes("s1", "2026-11-01T00:00:00Z")
        types = [(event["at"], event["type"]) for event in changes["items"]]
        self.assertIn(("2026-09-10T00:00:00Z", "version_effective"), types)
        self.assertIn(("2026-10-10T00:00:00Z", "version_expires"), types)
        self.assertIn(("2026-09-20T00:00:00Z", "override_starts"), types)
        self.assertIn(("2026-09-25T00:00:00Z", "override_expires"), types)
        self.assertEqual(types, sorted(types, key=lambda item: item[0]))

    def test_historical_task_reports_rule_sources(self):
        _, version_id = self.draft_published()
        self.service.add_override(request_id="ooa", actor_id="op1", site_id="s1", kind="add",
                                  item_code="EX01", reason="r", valid_from="2026-09-01",
                                  valid_to="2026-09-30",
                                  content={"category": "other", "content": "补项"})
        receipt = self.service.generate_daily_task(
            request_id="gg", actor_id="op1", site_id="s1", task_date="2026-09-10")
        task = self.service.get_task(receipt.resource_id)
        extra = next(item for item in task.items if item.code == "EX01")
        self.assertEqual("enterprise_override", extra.sources[0]["kind"])
        baseline = next(item for item in task.items if item.code == "WG01")
        self.assertEqual(version_id, baseline.sources[0]["version_id"])

    def test_get_unknown_task_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.get_task("missing")

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    def test_auditor_cannot_draft_or_publish(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_template(request_id="tt", actor_id="au1", name="清单")
        template_id = self.service.create_template(
            request_id="tt", actor_id="op1", name="清单").resource_id
        with self.assertRaises(PermissionDenied):
            self.service.create_template_version(
                request_id="vv", actor_id="au1", template_id=template_id,
                applicability={"process_profiles": ["spray_paint"], "enterprise_tags": []},
                items=ITEMS)

    def test_operator_cannot_review(self):
        _, version_id = self.draft_template()
        self.service.submit_version(request_id="ss", actor_id="op1", version_id=version_id)
        with self.assertRaises(PermissionDenied):
            self.service.review_version(request_id="rr", actor_id="op1",
                                        version_id=version_id, result="approved")


if __name__ == "__main__":
    unittest.main()
