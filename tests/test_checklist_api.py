import unittest

from inspection_catalog_core.api import route
from inspection_catalog_core.checklist_service import ChecklistService
from inspection_catalog_core.clock import FixedClock
from inspection_catalog_core.service import DomainService
from inspection_catalog_core.storage import Database

from datetime import datetime, timezone


ITEMS = [
    {"code": "GAS-01", "content": "废气治理", "category": "waste_gas",
     "applicable_processes": ["wood_spray"], "risk_conditions": ["waste_gas"]},
    {"code": "GEN-01", "content": "通用手续", "category": "general"},
]


class ChecklistApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, clock)
        self.checklist = ChecklistService(self.database, clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="街道")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                   display_name="业务员", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                   display_name="复核员", role="reviewer", organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="家具厂", timezone_name="Asia/Shanghai")
        self.domain.record_domain_data(request_id="p1", actor_id="op1", site_id="s1",
                                       category="process_profile", external_key="wood_spray",
                                       data={"name": "喷涂"})
        self.domain.record_domain_data(request_id="f1", actor_id="op1", site_id="s1",
                                       category="pollution_factor", external_key="waste_gas",
                                       data={"name": "废气"})

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.domain, method, path, body, {"X-Actor-Id": actor},
                     checklist=self.checklist)

    def publish(self, tpl_req="tpl"):
        _, payload = self.call("POST", "/checklist-templates",
                               {"request_id": tpl_req, "organization_id": "o1", "name": "模板"})
        template_id = payload["resource_id"]
        self.call("POST", "/checklist-versions",
                  {"request_id": "draft", "template_id": template_id, "name": "v1",
                   "items": ITEMS})
        status, listing = self.call("GET", f"/checklist-versions?template_id={template_id}")
        version_id = listing["items"][0]["version_id"]
        self.call("POST", f"/checklist-versions/{version_id}/submit", {"request_id": "sub"})
        self.call("POST", f"/checklist-versions/{version_id}/review",
                  {"request_id": "rev", "decision": "approved"}, actor="rv1")
        self.call("POST", f"/checklist-versions/{version_id}/publish",
                  {"request_id": "pub", "effective_from": "2026-09-25"})
        return template_id, version_id

    def test_full_publish_flow_over_http(self):
        template_id, version_id = self.publish()
        status, current = self.call("GET", "/checklist/current?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(version_id, current["version"]["version_id"])
        self.assertEqual(["GAS-01", "GEN-01"], [i["code"] for i in current["items"]])

    def test_current_requires_site_id(self):
        status, payload = self.call("GET", "/checklist/current")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_task_freeze_and_rule_sources_over_http(self):
        template_id, version_id = self.publish()
        status, created = self.call("POST", "/inspection-tasks",
                                    {"request_id": "task1", "site_id": "s1",
                                     "task_date": "2026-09-25"})
        self.assertEqual(201, status)
        task_id = created["resource_id"]
        status, _ = self.call("POST", f"/inspection-tasks/{task_id}/start")
        self.assertEqual(200, status)
        status, detail = self.call("GET", f"/inspection-tasks/{task_id}")
        self.assertEqual(200, status)
        self.assertEqual("started", detail["status"])
        status, sources = self.call("GET", f"/inspection-tasks/{task_id}/rule-sources")
        self.assertEqual(200, status)
        self.assertEqual(version_id, sources["version"]["version_id"])
        self.assertEqual("base", sources["items"][0]["sources"][0]["layer"])

    def test_overlay_and_revoke_over_http(self):
        template_id, _ = self.publish()
        status, created = self.call("POST", "/checklist-overlays", {
            "request_id": "ov1", "template_id": template_id, "site_id": "s1",
            "reason": "专项整改", "effective_from": "2026-09-25",
            "expires_at": "2026-10-25",
            "changes": [{"kind": "remove", "code": "GEN-01"}]})
        self.assertEqual(201, status)
        overlay_id = created["resource_id"]
        _, current = self.call("GET", "/checklist/current?site_id=s1")
        self.assertEqual(["GAS-01"], [i["code"] for i in current["items"]])
        status, _ = self.call("POST", f"/checklist-overlays/{overlay_id}/revoke",
                              {"request_id": "revoke1", "reason": "完成"})
        self.assertEqual(201, status)
        _, current = self.call("GET", "/checklist/current?site_id=s1")
        self.assertEqual(["GAS-01", "GEN-01"], [i["code"] for i in current["items"]])

    def test_future_changes_over_http(self):
        template_id, _ = self.publish()
        status, payload = self.call("GET", "/checklist/future-changes?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual([], payload["events"])

    def test_unknown_checklist_route_404_without_service(self):
        # 未挂载清单服务时返回 404 而不是崩溃
        status, payload = route(self.domain, "GET", "/checklist/current?site_id=s1", None)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
