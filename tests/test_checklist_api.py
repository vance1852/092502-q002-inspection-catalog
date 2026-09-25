import unittest
from datetime import datetime, timezone

from inspection_catalog_core.api import route
from inspection_catalog_core.checklist_service import ChecklistService
from inspection_catalog_core.clock import FixedClock
from inspection_catalog_core.service import DomainService
from inspection_catalog_core.storage import Database


ITEMS = [
    {"code": "WG01", "category": "waste_gas", "content": "废气治理设施运行"},
    {"code": "HW01", "category": "hazardous_waste", "content": "危废贮存台账"},
]
APPLICABILITY = {"process_profiles": ["spray_paint"], "enterprise_tags": ["high_risk"]}


class ChecklistApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="街道")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="业务员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                    display_name="复核员", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="家具厂",
                                   timezone_name="Asia/Shanghai")
        self.service.record_domain_data(request_id="pp", actor_id="op1", site_id="s1",
                                        category="process_profile", external_key="spray_paint",
                                        data={"name": "喷涂"})
        self.service.record_domain_data(request_id="tag", actor_id="op1", site_id="s1",
                                        category="enterprise_tag", external_key="high_risk",
                                        data={"name": "高风险"})
        self.checklists = ChecklistService(self.database, clock)

    def tearDown(self):
        self.database.close()

    def headers(self, actor_id="op1"):
        return {"X-Actor-Id": actor_id}

    def publish_one(self, request_prefix="x", from_="2026-09-01", to_=None):
        status, payload = route(self.service, "POST", "/checklist-templates",
                                {"request_id": f"{request_prefix}-t", "name": "清单"},
                                self.headers())
        template_id = payload["resource_id"]
        route(self.service, "POST", "/checklist-versions",
              {"request_id": f"{request_prefix}-v", "template_id": template_id,
               "applicability": APPLICABILITY, "items": ITEMS}, self.headers())
        version_id = self._latest_version_id(template_id)
        route(self.service, "POST", "/checklist-versions/submit",
              {"request_id": f"{request_prefix}-s", "version_id": version_id}, self.headers())
        route(self.service, "POST", "/checklist-versions/review",
              {"request_id": f"{request_prefix}-r", "version_id": version_id,
               "result": "approved"}, self.headers("rv1"))
        status, payload = route(self.service, "POST", "/checklist-versions/publish",
                                {"request_id": f"{request_prefix}-p", "version_id": version_id,
                                 "effective_from": from_, "effective_to": to_},
                                self.headers("rv1"))
        self.assertEqual(201, status, payload)
        return template_id, version_id

    def _latest_version_id(self, template_id):
        status, payload = route(self.service, "GET",
                                f"/checklist-versions?template_id={template_id}", None)
        self.assertEqual(200, status)
        return payload["items"][-1]["version_id"]

    def test_full_workflow_over_http(self):
        template_id, version_id = self.publish_one("w", "2026-09-01", "2026-12-31")

        # 当前清单
        status, payload = route(self.service, "GET",
                                "/checklist/current?site_id=s1&effective_at=2026-09-10", None)
        self.assertEqual(200, status)
        self.assertEqual(["HW01", "WG01"], [i["code"] for i in payload["items"]])
        self.assertEqual(version_id, payload["matched_versions"][0]["version_id"])

        # 企业覆盖
        status, payload = route(self.service, "POST", "/overrides",
                                {"request_id": "oa", "site_id": "s1", "kind": "remove",
                                 "item_code": "WG01", "reason": "废气工序停产",
                                 "valid_from": "2026-09-05", "valid_to": "2026-09-20"},
                                self.headers())
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET",
                                "/checklist/current?site_id=s1&effective_at=2026-09-10", None)
        self.assertEqual(["HW01"], [i["code"] for i in payload["items"]])
        self.assertEqual("WG01", payload["removals"][0]["item_code"])

        # 生成每日任务
        status, payload = route(self.service, "POST", "/daily-tasks",
                                {"request_id": "g1", "site_id": "s1",
                                 "task_date": "2026-09-10"}, self.headers())
        self.assertEqual(201, status)
        task_id = payload["resource_id"]

        # 开始任务
        status, payload = route(self.service, "POST", "/tasks/start",
                                {"task_id": task_id}, self.headers())
        self.assertEqual(200, status)

        # 历史任务溯源：冻结快照仍含被覆盖前的条目解析结果
        status, payload = route(self.service, "GET", f"/task?task_id={task_id}", None)
        self.assertEqual(200, status)
        self.assertEqual(["HW01"], [i["code"] for i in payload["items"]])
        self.assertEqual("started", payload["status"])
        self.assertEqual([version_id], payload["resolution"]["version_ids"])

        # 未来变更
        status, payload = route(self.service, "GET",
                                "/checklist/future?site_id=s1&within_to=2027-01-01", None)
        self.assertEqual(200, status)
        types = {event["type"] for event in payload["items"]}
        self.assertIn("override_expires", types)
        self.assertIn("version_expires", types)

    def test_overlap_publish_returns_conflict(self):
        template_id, _ = self.publish_one("a", "2026-09-01", "2026-10-01")
        # 起草第二个版本
        route(self.service, "POST", "/checklist-versions",
              {"request_id": "b-v", "template_id": template_id,
               "applicability": APPLICABILITY, "items": ITEMS}, self.headers())
        version_id = self._latest_version_id(template_id)
        route(self.service, "POST", "/checklist-versions/submit",
              {"request_id": "b-s", "version_id": version_id}, self.headers())
        route(self.service, "POST", "/checklist-versions/review",
              {"request_id": "b-r", "version_id": version_id, "result": "approved"},
              self.headers("rv1"))
        status, payload = route(self.service, "POST", "/checklist-versions/publish",
                                {"request_id": "b-p", "version_id": version_id,
                                 "effective_from": "2026-09-15", "effective_to": "2026-11-01"},
                                self.headers("rv1"))
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_self_review_returns_permission_denied(self):
        self.publish_one  # noqa: B018
        status, _ = route(self.service, "POST", "/checklist-templates",
                          {"request_id": "t1", "name": "清单"}, self.headers())
        template_id = _["resource_id"]
        route(self.service, "POST", "/checklist-versions",
              {"request_id": "v1", "template_id": template_id,
               "applicability": APPLICABILITY, "items": ITEMS}, self.headers())
        version_id = self._latest_version_id(template_id)
        route(self.service, "POST", "/checklist-versions/submit",
              {"request_id": "s1", "version_id": version_id}, self.headers())
        status, payload = route(self.service, "POST", "/checklist-versions/review",
                                {"request_id": "r1", "version_id": version_id,
                                 "result": "approved"}, self.headers())
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_missing_site_id_is_validation_error(self):
        status, payload = route(self.service, "GET", "/checklist/current", None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
