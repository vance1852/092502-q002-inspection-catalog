import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from inspection_catalog_core.checklist_service import ChecklistService
from inspection_catalog_core.clock import FixedClock
from inspection_catalog_core.errors import ConflictError
from inspection_catalog_core.service import DomainService
from inspection_catalog_core.storage import Database


ITEMS = [{"code": "WG01", "category": "waste_gas", "content": "废气治理"}]
APPLICABILITY = {"process_profiles": ["spray_paint"], "enterprise_tags": ["high_risk"]}


class ConcurrentApprovalTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "concurrent.sqlite3"
        bootstrap = Database(self.path)
        domain = DomainService(bootstrap, FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc)))
        domain.register_organization(request_id="org", actor_id="bootstrap",
                                     organization_id="o1", name="街道")
        domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                              display_name="管理员", role="admin", organization_id="o1")
        domain.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                              display_name="业务员", role="operator", organization_id="o1")
        domain.register_actor(request_id="rv1", actor_id="a1", new_actor_id="rv1",
                              display_name="复核员甲", role="reviewer", organization_id="o1")
        domain.register_actor(request_id="rv2", actor_id="a1", new_actor_id="rv2",
                              display_name="复核员乙", role="reviewer", organization_id="o1")
        domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                             organization_id="o1", name="家具厂", timezone_name="Asia/Shanghai")
        service = ChecklistService(bootstrap, domain.clock)
        self.template_id = service.create_template(
            request_id="tt", actor_id="op1", name="清单").resource_id
        self.version_ids = []
        for index in range(3):
            version_id = service.create_template_version(
                request_id=f"v{index}", actor_id="op1", template_id=self.template_id,
                applicability=APPLICABILITY, items=ITEMS).resource_id
            service.submit_version(request_id=f"s{index}", actor_id="op1",
                                   version_id=version_id)
            self.version_ids.append(version_id)
        bootstrap.close()

    def tearDown(self):
        self.directory.cleanup()

    def _service(self) -> ChecklistService:
        database = Database(self.path)
        return ChecklistService(database, FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc)))

    def test_concurrent_reviews_exactly_one_succeeds(self):
        version_id = self.version_ids[0]
        results: list[object] = []
        barrier = threading.Barrier(2)

        def worker(actor_id: str, request_id: str) -> None:
            service = self._service()
            barrier.wait()
            try:
                service.review_version(request_id=request_id, actor_id=actor_id,
                                       version_id=version_id, result="approved")
                results.append("ok")
            except ConflictError:
                results.append("conflict")
            except Exception as exc:  # pragma: no cover - 只允许出现确定冲突
                results.append(exc)
            finally:
                service.database.close()

        threads = [
            threading.Thread(target=worker, args=("rv1", "req-rv1")),
            threading.Thread(target=worker, args=("rv2", "req-rv2")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual({"ok", "conflict"}, set(results))
        service = self._service()
        version = service.get_version(version_id)
        self.assertEqual("approved", version.status)
        self.assertIn(version.reviewed_by, ("rv1", "rv2"))
        service.database.close()

    def test_concurrent_overlapping_publishes_exactly_one_succeeds(self):
        results: list[object] = []
        barrier = threading.Barrier(2)

        def worker(actor_id: str, request_id: str, version_id: str) -> None:
            service = self._service()
            barrier.wait()
            try:
                service.publish_version(request_id=request_id, actor_id=actor_id,
                                        version_id=version_id,
                                        effective_from="2026-09-01", effective_to="2026-12-31")
                results.append("ok")
            except ConflictError:
                results.append("conflict")
            except Exception as exc:  # pragma: no cover
                results.append(exc)
            finally:
                service.database.close()

        # 两个版本分别先复核通过（独立连接串行完成）
        for index, version_id in enumerate(self.version_ids[:2]):
            service = self._service()
            service.review_version(request_id=f"ra{index}", actor_id="rv1",
                                   version_id=version_id, result="approved")
            service.database.close()

        threads = [
            threading.Thread(target=worker, args=("rv1", "req-p1", self.version_ids[0])),
            threading.Thread(target=worker, args=("rv2", "req-p2", self.version_ids[1])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual({"ok", "conflict"}, set(results))
        service = self._service()
        published = [v for v in service.list_versions(self.template_id)
                     if v.status == "published"]
        self.assertEqual(1, len(published))
        service.database.close()


if __name__ == "__main__":
    unittest.main()
