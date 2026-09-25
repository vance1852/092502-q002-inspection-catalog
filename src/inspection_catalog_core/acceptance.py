"""运行基础服务的离线端到端验收。"""

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
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范企业")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="环保负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="清单复核员", role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="process_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="process_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        checklists = ChecklistService(database, service.clock)
        checklist_result = _run_checklist_chain(service, checklists)

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, **checklist_result}
        database.close()
        return result


def _run_checklist_chain(service: DomainService, checklists: ChecklistService) -> dict[str, object]:
    """演练差异化清单：起草、复核、发布、覆盖、冻结任务、撤销后历史不变。"""

    # 企业工艺与风险标签
    service.record_domain_data(request_id="req-pp-spray", actor_id="operator-001", site_id="site-001",
                               category="process_profile", external_key="spray_paint",
                               data={"name": "喷涂工艺"})
    service.record_domain_data(request_id="req-tag-risk", actor_id="operator-001", site_id="site-001",
                               category="enterprise_tag", external_key="high_risk",
                               data={"name": "高风险"})

    template = checklists.create_template(request_id="req-tpl", actor_id="operator-001",
                                          name="废气粉尘危废差异化清单")
    template_id = template.resource_id
    version = checklists.create_template_version(
        request_id="req-ver", actor_id="operator-001", template_id=template_id,
        applicability={"process_profiles": ["spray_paint"], "enterprise_tags": ["high_risk"]},
        items=[
            {"code": "WG01", "category": "waste_gas", "content": "VOCs 废气治理设施正常运行"},
            {"code": "DC01", "category": "dust_collection", "content": "粉尘收集装置定期清理"},
            {"code": "HW01", "category": "hazardous_waste", "content": "危废分区贮存并建立台账"},
        ])
    version_id = version.resource_id
    checklists.submit_version(request_id="req-submit", actor_id="operator-001", version_id=version_id)
    checklists.review_version(request_id="req-review", actor_id="reviewer-001",
                              version_id=version_id, result="approved")
    checklists.publish_version(request_id="req-publish", actor_id="reviewer-001",
                               version_id=version_id,
                               effective_from="2026-09-01", effective_to="2026-12-31")

    # 企业级覆盖：临时增补一项
    checklists.add_override(request_id="req-override", actor_id="operator-001", site_id="site-001",
                            kind="add", item_code="EX01", reason="近期信访投诉，加密夜间巡查",
                            valid_from="2026-09-20", valid_to="2026-10-20",
                            content={"category": "other", "content": "夜间巡查补项"})

    current = checklists.current_checklist("site-001", "2026-09-25T08:00:00Z")
    current_codes = [item["code"] for item in current["items"]]

    # 每日任务生成即冻结
    generated = checklists.generate_daily_task(request_id="req-task", actor_id="operator-001",
                                               site_id="site-001", task_date="2026-09-25")
    task_before = checklists.get_task(generated.resource_id)
    checklists.start_task(actor_id="operator-001", task_id=task_before.task_id)

    # 撤销版本：只影响此后生成的任务，已开始任务保持不变
    checklists.revoke_version(request_id="req-revoke", actor_id="reviewer-001",
                              version_id=version_id, reason="新规替代")
    task_after = checklists.get_task(generated.resource_id)
    frozen_unchanged = (
        [i.code for i in task_before.items] == [i.code for i in task_after.items]
        and task_before.snapshot_hash == task_after.snapshot_hash
        and task_after.status == "started"
    )
    future = checklists.future_changes("site-001", "2027-01-01T00:00:00Z")
    return {
        "checklist_current_items": current_codes,
        "frozen_task_items": [i.code for i in task_after.items],
        "frozen_task_version_ids": list(task_after.version_ids),
        "frozen_unchanged_after_revoke": frozen_unchanged,
        "future_change_events": len(future["items"]),
    }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
