"""运行基础服务与专属巡查清单服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .checklist_service import ChecklistService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记、发布、覆盖与任务冻结链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        checklist = ChecklistService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范企业")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="环保负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="复核员", role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="process_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="process_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 企业工艺与风险标签，驱动条目适用性
        service.record_domain_data(request_id="req-process", actor_id="operator-001", site_id="site-001",
                                   category="process_profile", external_key="wood_spray",
                                   data={"name": "木器喷涂"})
        service.record_domain_data(request_id="req-factor", actor_id="operator-001", site_id="site-001",
                                   category="pollution_factor", external_key="waste_gas",
                                   data={"name": "废气"})

        # 起草模板版本：废气治理按工艺+风险适用，通用条目无条件适用
        template = checklist.create_template(request_id="req-template", actor_id="operator-001",
                                             organization_id="org-001", name="差异化巡查清单")
        template_id = template.resource_id
        version = checklist.draft_version(request_id="req-draft", actor_id="operator-001",
                                          template_id=template_id, name="九月版", items=[
            {"code": "GAS-01", "content": "废气治理设施正常运行", "category": "waste_gas",
             "applicable_processes": ["wood_spray"], "risk_conditions": ["waste_gas"]},
            {"code": "GEN-01", "content": "环保手续齐全", "category": "general"},
        ])
        version_id = version.resource_id
        checklist.submit_version(request_id="req-submit", actor_id="operator-001", version_id=version_id)
        checklist.review_version(request_id="req-review", actor_id="reviewer-001",
                                 version_id=version_id, decision="approved", comment="同意发布")
        checklist.publish_version(request_id="req-publish", actor_id="operator-001",
                                  version_id=version_id, effective_from="2026-09-25")

        # 企业级有期限覆盖层：临时加严一条
        overlay = checklist.create_overlay(request_id="req-overlay", actor_id="operator-001",
                                           template_id=template_id, site_id="site-001",
                                           reason="废气专项整改", effective_from="2026-09-25",
                                           expires_at="2026-10-31", changes=[
            {"kind": "add", "code": "SP-01",
             "item": {"code": "SP-01", "content": "专项加严：夜间限产", "category": "special",
                      "risk_conditions": ["waste_gas"]}}])

        current = checklist.get_current_checklist("site-001")
        current_codes = [item["code"] for item in current["items"]]

        # 生成当日任务并开始，此后清单被冻结
        task = checklist.create_daily_task(request_id="req-task", actor_id="operator-001",
                                           site_id="site-001", task_date="2026-09-25")
        checklist.start_task(actor_id="operator-001", task_id=task.resource_id)
        frozen = checklist.get_task(task.resource_id)
        frozen_codes = [item["code"] for item in frozen["items"]]
        sources = checklist.get_task_rule_sources(task.resource_id)

        # 发布十月新版本，不得改写已开始的任务
        version2 = checklist.draft_version(request_id="req-draft2", actor_id="operator-001",
                                           template_id=template_id, name="十月版", items=[
            {"code": "GAS-01", "content": "废气治理设施正常运行", "category": "waste_gas",
             "applicable_processes": ["wood_spray"], "risk_conditions": ["waste_gas"]},
            {"code": "GEN-01", "content": "环保手续齐全", "category": "general"},
            {"code": "HW-01", "content": "危废贮存规范", "category": "hazardous_waste",
             "risk_conditions": ["hazardous_waste"]},
        ])
        checklist.submit_version(request_id="req-submit2", actor_id="operator-001",
                                 version_id=version2.resource_id)
        checklist.review_version(request_id="req-review2", actor_id="reviewer-001",
                                 version_id=version2.resource_id, decision="approved")
        checklist.publish_version(request_id="req-publish2", actor_id="operator-001",
                                  version_id=version2.resource_id, effective_from="2026-10-01")
        frozen_after = checklist.get_task(task.resource_id)
        frozen_codes_after = [item["code"] for item in frozen_after["items"]]

        future = checklist.get_future_changes("site-001")
        future_types = sorted({event["type"] for event in future["events"]})

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {
            "status": "ok",
            "records": len(records),
            "audit_events": event_count,
            "audit_valid": valid,
            "first_replayed": first.replayed,
            "second_replayed": replay.replayed,
            "current_codes": current_codes,
            "frozen_codes": frozen_codes,
            "frozen_codes_after_new_publish": frozen_codes_after,
            "frozen_version_id": frozen["version_id"],
            "frozen_manifest_hash": sources["manifest_hash"],
            "overlay_id": overlay.resource_id,
            "future_change_types": future_types,
            "tag_snapshot_keys": sorted(sources["enterprise_tag_snapshot"].keys()),
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = (result["status"] == "ok" and result["audit_valid"]
                and result["frozen_codes"] == result["frozen_codes_after_new_publish"]
                and result["current_codes"] == ["GAS-01", "GEN-01", "SP-01"])
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
