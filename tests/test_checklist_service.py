import threading
import unittest
from datetime import datetime, timezone

from inspection_catalog_core.checklist_service import ChecklistService
from inspection_catalog_core.clock import FixedClock
from inspection_catalog_core.errors import ConflictError, PermissionDenied, ValidationError
from inspection_catalog_core.service import DomainService
from inspection_catalog_core.storage import Database


def gas_items():
    return [
        {"code": "GAS-01", "content": "废气治理设施运行", "category": "waste_gas",
         "applicable_processes": ["wood_spray"], "risk_conditions": ["waste_gas"]},
        {"code": "DUST-01", "content": "粉尘收集", "category": "dust",
         "risk_conditions": ["dust"]},
        {"code": "HW-01", "content": "危废贮存规范", "category": "hazardous_waste",
         "risk_conditions": ["hazardous_waste"]},
        {"code": "GEN-01", "content": "通用环保手续", "category": "general"},
    ]


class ChecklistFixture(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
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
        self.domain.record_domain_data(request_id="dd-p1", actor_id="op1", site_id="s1",
                                       category="process_profile", external_key="wood_spray",
                                       data={"name": "木器喷涂"})
        self.domain.record_domain_data(request_id="dd-f1", actor_id="op1", site_id="s1",
                                       category="pollution_factor", external_key="waste_gas",
                                       data={"name": "废气"})
        self.domain.record_domain_data(request_id="dd-t1", actor_id="op1", site_id="s1",
                                       category="enterprise_tag", external_key="hazardous_waste",
                                       data={"name": "危废"})

    def tearDown(self):
        self.database.close()

    def create_published(self, request_prefix, items=None, effective_from="2026-09-25",
                         effective_until=None, name=None):
        template = self.service.create_template(
            request_id=f"{request_prefix}-tpl", actor_id="op1",
            organization_id="o1", name=name or "模板")
        template_id = template.resource_id
        version = self.service.draft_version(
            request_id=f"{request_prefix}-draft", actor_id="op1", template_id=template_id,
            name="v1", items=items or gas_items())
        version_id = version.resource_id
        self.service.submit_version(request_id=f"{request_prefix}-sub", actor_id="op1",
                                    version_id=version_id)
        self.service.review_version(request_id=f"{request_prefix}-rev", actor_id="rv1",
                                    version_id=version_id, decision="approved")
        self.service.publish_version(
            request_id=f"{request_prefix}-pub", actor_id="op1", version_id=version_id,
            effective_from=effective_from, effective_until=effective_until)
        return template_id, version_id


class LifecycleTest(ChecklistFixture):
    def test_full_draft_submit_review_publish_chain(self):
        template_id, version_id = self.create_published("lc")
        versions = self.service.list_versions(template_id)
        self.assertEqual("published", versions[0]["status"])
        self.assertEqual("approved", versions[0]["review_decision"])

    def test_duplicate_template_rejected(self):
        self.create_published("dup")
        with self.assertRaises(ConflictError):
            self.service.create_template(request_id="dup2", actor_id="op1",
                                         organization_id="o1", name="第二个")

    def test_reviewer_cannot_draft(self):
        template = self.service.create_template(request_id="t", actor_id="op1",
                                                organization_id="o1", name="m")
        with self.assertRaises(PermissionDenied):
            self.service.draft_version(request_id="d", actor_id="rv1",
                                       template_id=template.resource_id, name="x",
                                       items=gas_items())

    def test_cannot_publish_without_approval(self):
        template = self.service.create_template(request_id="t", actor_id="op1",
                                                organization_id="o1", name="m")
        version = self.service.draft_version(request_id="d", actor_id="op1",
                                             template_id=template.resource_id, name="v",
                                             items=gas_items())
        self.service.submit_version(request_id="s", actor_id="op1",
                                    version_id=version.resource_id)
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="p", actor_id="op1",
                                         version_id=version.resource_id,
                                         effective_from="2026-09-25")

    def test_four_eyes_principle_submitter_cannot_review(self):
        template = self.service.create_template(request_id="t", actor_id="op1",
                                                organization_id="o1", name="m")
        version = self.service.draft_version(request_id="d", actor_id="op1",
                                             template_id=template.resource_id, name="v",
                                             items=gas_items())
        # admin 也可以提交，但不能复核自己提交的版本
        self.service.submit_version(request_id="s", actor_id="a1",
                                    version_id=version.resource_id)
        with self.assertRaises(PermissionDenied):
            self.service.review_version(request_id="r", actor_id="a1",
                                        version_id=version.resource_id, decision="approved")

    def test_rejection_returns_to_draft_and_can_resubmit(self):
        template = self.service.create_template(request_id="t", actor_id="op1",
                                                organization_id="o1", name="m")
        version = self.service.draft_version(request_id="d", actor_id="op1",
                                             template_id=template.resource_id, name="v",
                                             items=gas_items())
        self.service.submit_version(request_id="s", actor_id="op1",
                                    version_id=version.resource_id)
        self.service.review_version(request_id="r", actor_id="rv1",
                                    version_id=version.resource_id,
                                    decision="rejected", comment="条目不全")
        versions = self.service.list_versions(template.resource_id)
        self.assertEqual("draft", versions[0]["status"])
        # 修改后重新走流程
        self.service.update_draft(request_id="u", actor_id="op1",
                                  version_id=version.resource_id, name="v改",
                                  items=gas_items())
        self.service.submit_version(request_id="s2", actor_id="op1",
                                    version_id=version.resource_id)
        self.service.review_version(request_id="r2", actor_id="rv1",
                                    version_id=version.resource_id, decision="approved")
        self.service.publish_version(request_id="p", actor_id="op1",
                                     version_id=version.resource_id,
                                     effective_from="2026-09-25")
        self.assertEqual("published", self.service.list_versions(template.resource_id)[0]["status"])

    def test_duplicate_submit_and_review_are_conflicts(self):
        _, version_id = self.create_published("x")
        with self.assertRaises(ConflictError):
            self.service.submit_version(request_id="sagain", actor_id="op1",
                                        version_id=version_id)

    def test_auditor_has_no_write_access(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_template(request_id="t", actor_id="au1",
                                         organization_id="o1", name="m")


class EffectiveRangeTest(ChecklistFixture):
    def _draft_next(self, template_id, prefix, items):
        v = self.service.draft_version(request_id=f"{prefix}-d", actor_id="op1",
                                       template_id=template_id, name=prefix, items=items)
        self.service.submit_version(request_id=f"{prefix}-s", actor_id="op1",
                                    version_id=v.resource_id)
        self.service.review_version(request_id=f"{prefix}-r", actor_id="rv1",
                                    version_id=v.resource_id, decision="approved")
        return v.resource_id

    def test_overlapping_open_ranges_rejected(self):
        template_id, v1 = self.create_published("r1", effective_from="2026-09-25")
        v2 = self._draft_next(template_id, "r2", gas_items())
        # 与当前开放版本同日生效即重叠，必须严格晚于
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="r2p", actor_id="op1",
                                         version_id=v2, effective_from="2026-09-25")

    def test_successive_open_version_closes_previous(self):
        template_id, v1 = self.create_published("r1", effective_from="2026-09-25")
        v2 = self._draft_next(template_id, "r2", gas_items())
        self.service.publish_version(request_id="r2p", actor_id="op1",
                                     version_id=v2, effective_from="2026-10-01")
        versions = {v["version_id"]: v for v in self.service.list_versions(template_id)}
        self.assertEqual("2026-10-01", versions[v1]["effective_until"])
        self.assertIsNone(versions[v2]["effective_until"])

    def test_duplicate_publish_rejected(self):
        _, v1 = self.create_published("r1")
        with self.assertRaises(ConflictError):
            self.service.publish_version(request_id="again", actor_id="op1",
                                         version_id=v1, effective_from="2026-11-01")

    def test_retroactive_publish_rejected(self):
        template = self.service.create_template(request_id="t", actor_id="op1",
                                                organization_id="o1", name="m")
        v = self._draft_next(template.resource_id, "x", gas_items())
        with self.assertRaises(ValidationError):
            self.service.publish_version(request_id="p", actor_id="op1", version_id=v,
                                         effective_from="2026-09-01")

    def test_finite_window_must_not_overlap(self):
        template_id, v1 = self.create_published("w1", effective_from="2026-09-25",
                                                effective_until="2026-10-01")
        # 第一个开放版本接续窗口
        v_open = self._draft_next(template_id, "wo", gas_items())
        self.service.publish_version(request_id="wop", actor_id="op1",
                                     version_id=v_open, effective_from="2026-10-01")
        # 有限窗口与已有开放版本不能并存
        v3 = self._draft_next(template_id, "w3", gas_items())
        with self.assertRaises(ValidationError):
            self.service.publish_version(request_id="w3p", actor_id="op1", version_id=v3,
                                         effective_from="2026-11-01",
                                         effective_until="2026-11-10")


class RollbackTest(ChecklistFixture):
    def test_rollback_only_affects_ungenerated_tasks(self):
        template_id, v1 = self.create_published("rb", effective_from="2026-09-25")
        # 9-25 任务已生成并开始，冻结在 v1
        task = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                              site_id="s1", task_date="2026-09-25")
        self.service.start_task(actor_id="op1", task_id=task.resource_id)
        # 发布 v2 自 10-01
        v2_d = self.service.draft_version(request_id="v2d", actor_id="op1",
                                          template_id=template_id, name="v2",
                                          items=gas_items()[:3])
        self.service.submit_version(request_id="v2s", actor_id="op1", version_id=v2_d.resource_id)
        self.service.review_version(request_id="v2r", actor_id="rv1",
                                    version_id=v2_d.resource_id, decision="approved")
        self.service.publish_version(request_id="v2p", actor_id="op1",
                                     version_id=v2_d.resource_id, effective_from="2026-10-01")
        # 回滚 v1
        self.service.rollback_version(request_id="rb1", actor_id="rv1",
                                      version_id=v1, reason="条目有误")
        # 已开始任务不受影响（企业无 dust 标签，v1 适用 3 条）
        frozen = self.service.get_task(task.resource_id)
        self.assertEqual(v1, frozen["version_id"])
        self.assertEqual(3, len(frozen["items"]))
        # 回滚后的当前清单：空档回退…… 9-25 当天 v1 已回滚，v2 尚未生效
        # -> 解析报冲突（无可适用版本），这正是确定行为
        with self.assertRaises(ConflictError):
            self.service.get_current_checklist("s1", "2026-09-26")

    def test_rollback_future_version_restores_previous_for_new_tasks(self):
        template_id, v1 = self.create_published("rf", effective_from="2026-09-25")
        v2 = self.service.draft_version(request_id="v2d", actor_id="op1",
                                        template_id=template_id, name="v2",
                                        items=gas_items()[:3])
        self.service.submit_version(request_id="v2s", actor_id="op1", version_id=v2.resource_id)
        self.service.review_version(request_id="v2r", actor_id="rv1",
                                    version_id=v2.resource_id, decision="approved")
        self.service.publish_version(request_id="v2p", actor_id="op1",
                                     version_id=v2.resource_id, effective_from="2026-10-01")
        # 回滚尚未生效的 v2
        self.service.rollback_version(request_id="rb", actor_id="rv1",
                                      version_id=v2.resource_id, reason="暂缓")
        # 10-01 新任务应延续 v1（fallback_after_rollback）
        current = self.service.get_current_checklist("s1", "2026-10-01")
        self.assertEqual(v1, current["version"]["version_id"])
        self.assertTrue(current["version"]["fallback_after_rollback"])
        self.assertEqual(3, len(current["items"]))

    def test_double_rollback_conflict(self):
        _, v1 = self.create_published("rd")
        self.service.rollback_version(request_id="rb", actor_id="rv1",
                                      version_id=v1, reason="x")
        with self.assertRaises(ConflictError):
            self.service.rollback_version(request_id="rb2", actor_id="rv1",
                                          version_id=v1, reason="y")

    def test_operator_cannot_rollback(self):
        _, v1 = self.create_published("ro")
        with self.assertRaises(PermissionDenied):
            self.service.rollback_version(request_id="rb", actor_id="op1",
                                          version_id=v1, reason="x")


class OverlayTest(ChecklistFixture):
    def setUp(self):
        super().setUp()
        self.template_id, self.version_id = self.create_published("ov")

    def _overlay(self, prefix, changes, **kwargs):
        params = {"request_id": prefix, "actor_id": "op1", "template_id": self.template_id,
                  "site_id": "s1", "reason": "专项整改",
                  "effective_from": "2026-09-25", "expires_at": "2026-10-25"}
        params.update(kwargs)
        return self.service.create_overlay(changes=changes, **params)

    def test_overlay_requires_reason_and_period(self):
        with self.assertRaises(ValidationError):
            self.service.create_overlay(request_id="bad", actor_id="op1",
                                        template_id=self.template_id, site_id="s1",
                                        reason="   ", effective_from="2026-09-25",
                                        expires_at="2026-10-25",
                                        changes=[{"kind": "remove", "code": "GEN-01"}])
        with self.assertRaises(ValidationError):
            self.service.create_overlay(request_id="bad2", actor_id="op1",
                                        template_id=self.template_id, site_id="s1",
                                        reason="r", effective_from="2026-10-25",
                                        expires_at="2026-10-01",
                                        changes=[{"kind": "remove", "code": "GEN-01"}])

    def test_remove_and_modify_and_add(self):
        self._overlay("o1", [
            {"kind": "remove", "code": "GEN-01"},
            {"kind": "modify", "code": "HW-01", "content": "危废加严"},
            {"kind": "add", "code": "SP-01",
             "item": {"code": "SP-01", "content": "临时专项", "category": "special",
                      "risk_conditions": ["waste_gas"]}},
        ])
        current = self.service.get_current_checklist("s1")
        codes = [i["code"] for i in current["items"]]
        self.assertNotIn("GEN-01", codes)
        hw = next(i for i in current["items"] if i["code"] == "HW-01")
        self.assertEqual("危废加严", hw["content"])
        self.assertIn("SP-01", codes)

    def test_add_without_condition_rejected(self):
        with self.assertRaises(ValidationError):
            self._overlay("bad", [{"kind": "add", "code": "SP-02",
                                   "item": {"code": "SP-02", "content": "x", "category": "c"}}])

    def test_remove_unknown_code_rejected(self):
        with self.assertRaises(ValidationError):
            self._overlay("bad", [{"kind": "remove", "code": "NOPE"}])

    def test_revision_monotonic(self):
        self._overlay("o1", [{"kind": "remove", "code": "GEN-01"}])
        receipt = self._overlay("o2", [{"kind": "remove", "code": "HW-01"}])
        overlays = self.service.list_overlays(self.template_id, "s1")
        self.assertEqual([1, 2], [o["revision"] for o in overlays])
        self.assertEqual(receipt.resource_id, overlays[1]["overlay_id"])

    def test_revoke_only_affects_ungenerated_tasks(self):
        overlay = self._overlay("o1", [{"kind": "remove", "code": "GEN-01"}])
        task = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                              site_id="s1", task_date="2026-09-25")
        self.service.start_task(actor_id="op1", task_id=task.resource_id)
        self.service.revoke_overlay(request_id="revoke-ov", actor_id="op1",
                                    overlay_id=overlay.resource_id, reason="整改完成")
        # 已开始任务仍然没有 GEN-01
        frozen = self.service.get_task(task.resource_id)
        self.assertNotIn("GEN-01", [i["code"] for i in frozen["items"]])
        # 新一天的清单恢复 GEN-01
        current = self.service.get_current_checklist("s1", "2026-09-26")
        self.assertIn("GEN-01", [i["code"] for i in current["items"]])

    def test_expired_overlay_not_applied(self):
        self._overlay("o1", [{"kind": "remove", "code": "GEN-01"}],
                      effective_from="2026-09-25", expires_at="2026-10-01")
        current = self.service.get_current_checklist("s1", "2026-10-01")
        self.assertIn("GEN-01", [i["code"] for i in current["items"]])

    def test_double_revoke_conflict(self):
        overlay = self._overlay("o1", [{"kind": "remove", "code": "GEN-01"}])
        self.service.revoke_overlay(request_id="rv1", actor_id="op1",
                                    overlay_id=overlay.resource_id, reason="x")
        with self.assertRaises(ConflictError):
            self.service.revoke_overlay(request_id="rv2", actor_id="op1",
                                        overlay_id=overlay.resource_id, reason="y")


class TaskFreezeTest(ChecklistFixture):
    def test_task_freezes_items_sources_and_tag_snapshot(self):
        template_id, version_id = self.create_published("tf")
        self.service.create_overlay(request_id="ov", actor_id="op1", template_id=template_id,
                                    site_id="s1", reason="专项", effective_from="2026-09-25",
                                    expires_at="2026-12-31",
                                    changes=[{"kind": "remove", "code": "GEN-01"}])
        task = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                              site_id="s1", task_date="2026-09-25")
        detail = self.service.get_task(task.resource_id)
        codes = [i["code"] for i in detail["items"]]
        self.assertEqual(["GAS-01", "HW-01"], codes)
        sources = self.service.get_task_rule_sources(task.resource_id)
        self.assertEqual(version_id, sources["version"]["version_id"])
        self.assertEqual(1, len(sources["active_overlay_snapshot"]))
        self.assertIn("wood_spray",
                      [t["external_key"] for t in sources["enterprise_tag_snapshot"]["process_profile"]])

    def test_duplicate_daily_task_conflict_and_idempotent_replay(self):
        self.create_published("td")
        first = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                               site_id="s1", task_date="2026-09-25")
        replay = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                                site_id="s1", task_date="2026-09-25")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.service.create_daily_task(request_id="t2", actor_id="op1",
                                           site_id="s1", task_date="2026-09-25")

    def test_started_task_immune_to_later_version_and_tag_change(self):
        template_id, v1 = self.create_published("ti")
        task = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                              site_id="s1", task_date="2026-09-25")
        self.service.start_task(actor_id="op1", task_id=task.resource_id)
        # 企业标签变化：新增粉尘要素
        self.domain.record_domain_data(request_id="dust", actor_id="op1", site_id="s1",
                                       category="pollution_factor", external_key="dust",
                                       data={"name": "粉尘"})
        # 发布新版本
        v2 = self.service.draft_version(request_id="v2d", actor_id="op1",
                                        template_id=template_id, name="v2",
                                        items=gas_items())
        self.service.submit_version(request_id="v2s", actor_id="op1", version_id=v2.resource_id)
        self.service.review_version(request_id="v2r", actor_id="rv1",
                                    version_id=v2.resource_id, decision="approved")
        self.service.publish_version(request_id="v2p", actor_id="op1",
                                     version_id=v2.resource_id, effective_from="2026-09-26")
        frozen = self.service.get_task(task.resource_id)
        self.assertEqual(v1, frozen["version_id"])
        self.assertEqual(["GAS-01", "HW-01", "GEN-01"],
                         [i["code"] for i in frozen["items"]])
        # 新任务反映新标签
        task2 = self.service.create_daily_task(request_id="t2", actor_id="op1",
                                               site_id="s1", task_date="2026-09-26")
        detail2 = self.service.get_task(task2.resource_id)
        self.assertIn("DUST-01", [i["code"] for i in detail2["items"]])
        self.assertEqual(v2.resource_id, detail2["version_id"])

    def test_manifest_hash_stable_and_recorded(self):
        _, v1 = self.create_published("th")
        task = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                              site_id="s1", task_date="2026-09-25")
        sources = self.service.get_task_rule_sources(task.resource_id)
        self.assertEqual(64, len(sources["manifest_hash"]))

    def test_start_is_idempotent_and_complete_requires_start(self):
        _, v1 = self.create_published("ts")
        task = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                              site_id="s1", task_date="2026-09-25")
        r1 = self.service.start_task(actor_id="op1", task_id=task.resource_id)
        r2 = self.service.start_task(actor_id="op1", task_id=task.resource_id)
        self.assertEqual(("started", "started"), (r1["status"], r2["status"]))
        with self.assertRaises(ConflictError):
            pending = self.service.create_daily_task(request_id="t2", actor_id="op1",
                                                     site_id="s1", task_date="2026-09-26")
            self.service.complete_task(actor_id="op1", task_id=pending.resource_id)


class QueryTest(ChecklistFixture):
    def test_current_checklist_filters_by_process_and_risk(self):
        self.create_published("q1")
        current = self.service.get_current_checklist("s1")
        # 企业没有 dust 标签：DUST-01 不适用
        self.assertEqual(["GAS-01", "HW-01", "GEN-01"],
                         [i["code"] for i in current["items"]])

    def test_future_changes_lists_version_and_overlay_events(self):
        template_id, _ = self.create_published("q2", effective_from="2026-09-25")
        v2 = self.service.draft_version(request_id="d", actor_id="op1",
                                        template_id=template_id, name="v2", items=gas_items())
        self.service.submit_version(request_id="s", actor_id="op1", version_id=v2.resource_id)
        self.service.review_version(request_id="r", actor_id="rv1",
                                    version_id=v2.resource_id, decision="approved")
        self.service.publish_version(request_id="p", actor_id="op1",
                                     version_id=v2.resource_id, effective_from="2026-11-01")
        self.service.create_overlay(request_id="ov", actor_id="op1", template_id=template_id,
                                    site_id="s1", reason="r", effective_from="2026-10-10",
                                    expires_at="2026-11-10",
                                    changes=[{"kind": "remove", "code": "GEN-01"}])
        changes = self.service.get_future_changes("s1", from_date="2026-09-25")
        types = [(e["type"], e["effective_at"]) for e in changes["events"]]
        self.assertIn(("overlay_effective", "2026-10-10"), types)
        self.assertIn(("version_effective", "2026-11-01"), types)
        self.assertIn(("overlay_expiry", "2026-11-10"), types)
        # 事件按日期排序
        dates = [e["effective_at"] for e in changes["events"]]
        self.assertEqual(dates, sorted(dates))

    def test_historical_task_sources_query(self):
        _, v1 = self.create_published("q3")
        task = self.service.create_daily_task(request_id="t1", actor_id="op1",
                                              site_id="s1", task_date="2026-09-25")
        sources = self.service.get_task_rule_sources(task.resource_id)
        self.assertEqual("2026-09-25", sources["task_date"])
        self.assertTrue(all("sources" in i for i in sources["items"]))
        base_item = next(i for i in sources["items"] if i["code"] == "GAS-01")
        self.assertEqual("base", base_item["sources"][0]["layer"])


class IdempotentReplayTest(ChecklistFixture):
    def _approved(self, template_id, prefix):
        v = self.service.draft_version(request_id=f"{prefix}-d", actor_id="op1",
                                       template_id=template_id, name=prefix, items=gas_items())
        self.service.submit_version(request_id=f"{prefix}-s", actor_id="op1",
                                    version_id=v.resource_id)
        self.service.review_version(request_id=f"{prefix}-r", actor_id="rv1",
                                    version_id=v.resource_id, decision="approved")
        return v.resource_id

    def test_replay_submit_after_version_left_draft(self):
        t = self.service.create_template(request_id="t", actor_id="op1",
                                         organization_id="o1", name="m")
        vid = self._approved(t.resource_id, "x")
        # 版本已发布后，用原提交请求号重试应回放，而不是报"已经提交"
        self.service.publish_version(request_id="p", actor_id="op1", version_id=vid,
                                     effective_from="2026-09-25")
        replay = self.service.submit_version(request_id="x-s", actor_id="op1", version_id=vid)
        self.assertTrue(replay.replayed)

    def test_replay_publish_after_published(self):
        t = self.service.create_template(request_id="t", actor_id="op1",
                                         organization_id="o1", name="m")
        vid = self._approved(t.resource_id, "x")
        self.service.publish_version(request_id="p", actor_id="op1", version_id=vid,
                                     effective_from="2026-09-25")
        replay = self.service.publish_version(request_id="p", actor_id="op1", version_id=vid,
                                              effective_from="2026-09-25")
        self.assertTrue(replay.replayed)

    def test_replay_review_then_different_request_conflicts(self):
        t = self.service.create_template(request_id="t", actor_id="op1",
                                         organization_id="o1", name="m")
        vid = self._approved(t.resource_id, "x")
        replay = self.service.review_version(request_id="x-r", actor_id="rv1",
                                             version_id=vid, decision="approved")
        self.assertTrue(replay.replayed)
        with self.assertRaises(ConflictError):
            self.service.review_version(request_id="other", actor_id="rv1",
                                        version_id=vid, decision="approved")

    def test_replay_task_after_tag_change_returns_original(self):
        template_id, _ = self.create_published("ir")
        first = self.service.create_daily_task(request_id="task-x", actor_id="op1",
                                               site_id="s1", task_date="2026-09-25")
        # 标签随后变化，同号重试必须回放原冻结任务而不是重新解析
        self.domain.record_domain_data(request_id="dust", actor_id="op1", site_id="s1",
                                       category="pollution_factor", external_key="dust",
                                       data={"name": "粉尘"})
        replay = self.service.create_daily_task(request_id="task-x", actor_id="op1",
                                                site_id="s1", task_date="2026-09-25")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        detail = self.service.get_task(first.resource_id)
        self.assertNotIn("DUST-01", [i["code"] for i in detail["items"]])


class ConcurrencyTest(ChecklistFixture):
    def test_concurrent_reviews_one_wins(self):
        template = self.service.create_template(request_id="t", actor_id="op1",
                                                organization_id="o1", name="m")
        v = self.service.draft_version(request_id="d", actor_id="op1",
                                       template_id=template.resource_id, name="v",
                                       items=gas_items())
        self.service.submit_version(request_id="s", actor_id="op1", version_id=v.resource_id)
        results = []

        def approve(prefix):
            try:
                self.service.review_version(request_id=prefix, actor_id="rv1",
                                            version_id=v.resource_id, decision="approved")
                results.append("ok")
            except ConflictError:
                results.append("conflict")

        threads = [threading.Thread(target=approve, args=(f"r{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn("ok", results)
        self.assertIn("conflict", results)

    def test_concurrent_task_creation_single_winner(self):
        self.create_published("cc")
        winners = []

        def create(prefix):
            try:
                receipt = self.service.create_daily_task(request_id=prefix, actor_id="op1",
                                                         site_id="s1", task_date="2026-09-25")
                winners.append(receipt.resource_id)
            except ConflictError:
                pass

        threads = [threading.Thread(target=create, args=(f"t{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, len(winners))


if __name__ == "__main__":
    unittest.main()
