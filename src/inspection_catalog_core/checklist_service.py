"""实现专属巡查清单的模板版本、生效发布、企业覆盖层与任务冻结。

所有写操作都在 BEGIN IMMEDIATE 事务中完成：并发提交、并发复核、并发发布
在 SQLite 层串行化，后到者依据已落库状态得到确定的冲突结果。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .checklist_engine import change_from_dict, item_from_dict, resolve_checklist
from .checklist_models import (
    OVERLAY_ACTIVE,
    OVERLAY_REVOKED,
    TASK_COMPLETED,
    TASK_PENDING,
    TASK_STARTED,
    VERSION_DRAFT,
    VERSION_PENDING_REVIEW,
    VERSION_PUBLISHED,
    VERSION_ROLLED_BACK,
    ChecklistItem,
    ChecklistOverlay,
    ChecklistVersion,
    OverlayChange,
)
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database

FAR_FUTURE = "9999-12-31"


class ChecklistService:
    """提供清单起草、复核、发布、覆盖与任务生成的完整用例。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _date(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc

    def _text(self, value: Any, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _template_for_org(self, connection, organization_id: str):
        return connection.execute(
            "SELECT * FROM checklist_templates WHERE organization_id=?", (organization_id,)
        ).fetchone()

    def _template_row(self, connection, template_id: str):
        row = connection.execute(
            "SELECT * FROM checklist_templates WHERE template_id=?", (template_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("清单模板不存在")
        return row

    def _version_row(self, connection, version_id: str):
        row = connection.execute(
            "SELECT * FROM checklist_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("清单版本不存在")
        return row

    def _check_org(self, actor: Actor, organization_id: str) -> None:
        if actor.role != "admin" and actor.organization_id != organization_id:
            raise PermissionDenied("不能操作其他组织的清单")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._text(request_id, "request_id", 64)
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _items_from_payload(self, raw_items: Any) -> list[ChecklistItem]:
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("items 必须是非空数组")
        if len(raw_items) > 500:
            raise ValidationError("单个版本条目不能超过 500 条")
        items: list[ChecklistItem] = []
        seen: set[str] = set()
        for raw in raw_items:
            try:
                item = item_from_dict(raw)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if item.code in seen:
                raise ValidationError(f"条目 code 重复：{item.code}")
            seen.add(item.code)
            items.append(item)
        return items

    def _changes_from_payload(self, raw_changes: Any) -> list[OverlayChange]:
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ValidationError("changes 必须是非空数组")
        changes: list[OverlayChange] = []
        seen: set[str] = set()
        for raw in raw_changes:
            try:
                change = change_from_dict(raw)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            changes.append(change)
            seen.add(change.code)
        return changes

    def _version_from_row(self, row) -> ChecklistVersion:
        return ChecklistVersion(
            row["version_id"], row["template_id"], row["version_number"], row["status"],
            row["name"], tuple(item_from_dict(d) for d in json.loads(row["items_json"])),
            row["items_hash"], row["effective_from"], row["effective_until"],
            row["created_by"], row["created_at"], row["submitted_by"], row["submitted_at"],
            row["reviewed_by"], row["reviewed_at"], row["review_decision"], row["review_comment"],
            row["published_at"], row["rolled_back_by"], row["rolled_back_at"], row["rollback_reason"],
        )

    def _overlay_from_row(self, row) -> ChecklistOverlay:
        return ChecklistOverlay(
            row["overlay_id"], row["template_id"], row["site_id"], row["revision"],
            row["reason"], row["effective_from"], row["expires_at"], row["status"],
            tuple(change_from_dict(d) for d in json.loads(row["changes_json"])),
            row["created_by"], row["created_at"], row["revoked_by"], row["revoked_at"],
            row["revoke_reason"],
        )

    # ------------------------------------------------------------------ 模板

    def create_template(self, *, request_id: str, actor_id: str,
                        organization_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._check_org(actor, organization_id)
            name = self._text(name, "name")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                # 唯一性检查放在回调内，同 request_id 重试回放而非误报重复。
                if self._template_for_org(connection, organization_id) is not None:
                    raise ConflictError("该组织已经存在巡查清单模板")
                template_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO checklist_templates(template_id,organization_id,name,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (template_id, organization_id, name, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="checklist_template.created",
                             resource_type="checklist_template", resource_id=template_id,
                             detail={"organization_id": organization_id, "name": name},
                             occurred_at=self._now())
                return "checklist_template", template_id, {"template_id": template_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_checklist_template", payload=payload, create=create)

    def get_template(self, organization_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM checklist_templates WHERE organization_id=?", (organization_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("清单模板不存在")
            return {"template_id": row["template_id"], "organization_id": row["organization_id"],
                    "name": row["name"], "created_by": row["created_by"], "created_at": row["created_at"]}

    # ------------------------------------------------------------------ 版本起草与复核

    def draft_version(self, *, request_id: str, actor_id: str, template_id: str,
                      name: str, items: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "template_id": template_id, "name": name, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            template = self._template_row(connection, template_id)
            self._check_org(actor, template["organization_id"])
            name = self._text(name, "name")
            parsed_items = self._items_from_payload(items)
            items_data = [item.to_dict() for item in parsed_items]
            items_hash = digest(items_data)

            def create() -> tuple[str, str, dict[str, Any]]:
                number_row = connection.execute(
                    "SELECT COALESCE(MAX(version_number),0)+1 AS next FROM checklist_versions WHERE template_id=?",
                    (template_id,),
                ).fetchone()
                version_number = number_row["next"]
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO checklist_versions(version_id,template_id,version_number,status,name,"
                    "items_json,items_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (version_id, template_id, version_number, VERSION_DRAFT, name,
                     canonical_json(items_data), items_hash, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.drafted",
                             resource_type="checklist_version", resource_id=version_id,
                             detail={"template_id": template_id, "version_number": version_number,
                                     "items_hash": items_hash, "item_count": len(items_data)},
                             occurred_at=self._now())
                return "checklist_version", version_id, {"version_id": version_id,
                                                          "version_number": version_number}

            return self._idempotent(connection, request_id=request_id,
                                    action="draft_checklist_version", payload=payload, create=create)

    def update_draft(self, *, request_id: str, actor_id: str, version_id: str,
                     name: str, items: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id, "name": name, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version = self._version_row(connection, version_id)
            template = self._template_row(connection, version["template_id"])
            self._check_org(actor, template["organization_id"])
            name = self._text(name, "name")
            parsed_items = self._items_from_payload(items)
            items_data = [item.to_dict() for item in parsed_items]
            items_hash = digest(items_data)

            def create() -> tuple[str, str, dict[str, Any]]:
                if self._version_row(connection, version_id)["status"] != VERSION_DRAFT:
                    raise ConflictError("只有草稿状态的版本可以修改")
                connection.execute(
                    "UPDATE checklist_versions SET name=?, items_json=?, items_hash=? WHERE version_id=?",
                    (name, canonical_json(items_data), items_hash, version_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.draft_updated",
                             resource_type="checklist_version", resource_id=version_id,
                             detail={"items_hash": items_hash, "item_count": len(items_data)},
                             occurred_at=self._now())
                return "checklist_version", version_id, {"version_id": version_id,
                                                          "version_number": version["version_number"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="update_checklist_draft", payload=payload, create=create)

    def submit_version(self, *, request_id: str, actor_id: str, version_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            version = self._version_row(connection, version_id)
            template = self._template_row(connection, version["template_id"])
            self._check_org(actor, template["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                # 状态守卫放在回调内：同 request_id 重试直接重放回执，
                # 不会因状态已推进而被误判为重复提交。
                if version["status"] != VERSION_DRAFT:
                    raise ConflictError("版本已经提交复核或已发布")
                connection.execute(
                    "UPDATE checklist_versions SET status=?, submitted_by=?, submitted_at=?, "
                    "reviewed_by=NULL, reviewed_at=NULL, review_decision=NULL, review_comment=NULL "
                    "WHERE version_id=?",
                    (VERSION_PENDING_REVIEW, actor_id, self._now(), version_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.submitted",
                             resource_type="checklist_version", resource_id=version_id,
                             detail={"version_number": version["version_number"]},
                             occurred_at=self._now())
                return "checklist_version", version_id, {"version_id": version_id,
                                                          "status": VERSION_PENDING_REVIEW}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_checklist_version", payload=payload, create=create)

    def review_version(self, *, request_id: str, actor_id: str, version_id: str,
                       decision: str, comment: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "decision": decision, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            if decision not in {"approved", "rejected"}:
                raise ValidationError("decision 只能是 approved 或 rejected")
            version = self._version_row(connection, version_id)
            template = self._template_row(connection, version["template_id"])
            self._check_org(actor, template["organization_id"])
            comment = str(comment or "").strip()[:500]
            new_status = VERSION_PENDING_REVIEW if decision == "approved" else VERSION_DRAFT

            def create() -> tuple[str, str, dict[str, Any]]:
                if version["status"] != VERSION_PENDING_REVIEW or version["review_decision"] is not None:
                    raise ConflictError("版本已完成复核，重复复核不会生效")
                # 四眼原则：起草提交人不能复核自己的版本。
                if version["submitted_by"] == actor_id:
                    raise PermissionDenied("提交人不能复核自己起草的版本")
                connection.execute(
                    "UPDATE checklist_versions SET reviewed_by=?, reviewed_at=?, review_decision=?, "
                    "review_comment=?, status=? WHERE version_id=?",
                    (actor_id, self._now(), decision, comment, new_status, version_id),
                )
                append_event(connection, actor_id=actor_id,
                             action=f"checklist_version.review_{decision}",
                             resource_type="checklist_version", resource_id=version_id,
                             detail={"version_number": version["version_number"], "comment": comment},
                             occurred_at=self._now())
                return "checklist_version", version_id, {"version_id": version_id,
                                                          "review_decision": decision}

            return self._idempotent(connection, request_id=request_id,
                                    action="review_checklist_version", payload=payload, create=create)

    # ------------------------------------------------------------------ 发布与回滚

    def publish_version(self, *, request_id: str, actor_id: str, version_id: str,
                        effective_from: str, effective_until: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "effective_from": effective_from, "effective_until": effective_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version = self._version_row(connection, version_id)
            template = self._template_row(connection, version["template_id"])
            self._check_org(actor, template["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                # 状态、时钟与区间判定只在首次创建时执行；同 request_id 重试直接回放。
                current = self._version_row(connection, version_id)
                if current["status"] == VERSION_PUBLISHED:
                    raise ConflictError("版本已经发布，重复发布不会生效")
                if current["status"] == VERSION_ROLLED_BACK:
                    raise ConflictError("版本已经回滚，不能再次发布")
                if current["status"] != VERSION_PENDING_REVIEW or current["review_decision"] != "approved":
                    raise ConflictError("版本必须经复核通过后才能发布")
                start = self._date(effective_from, "effective_from")
                end: str | None = None
                if effective_until is not None:
                    end = self._date(effective_until, "effective_until")
                    if not start < end:
                        raise ValidationError("effective_until 必须晚于 effective_from")
                if start < self._today():
                    raise ValidationError("生效日期不能早于今天，禁止追溯发布")

                # 区间判定只看仍在发布状态的版本；已回滚版本退出时间轴，
                # 其留下的空档由解析层的 fallback 延续规则处理，不再限制新版本。
                published = connection.execute(
                    "SELECT * FROM checklist_versions WHERE template_id=? AND status=? "
                    "ORDER BY effective_from",
                    (version["template_id"], VERSION_PUBLISHED),
                ).fetchall()
                open_rows = [o for o in published if o["effective_until"] is None]
                closed_rows = [o for o in published if o["effective_until"] is not None]
                if len(open_rows) > 1:
                    raise ConflictError("存在多个开放生效区间，数据状态异常")
                open_row = open_rows[0] if open_rows else None
                # 与每个既有闭区间 [from, until) 不得重叠；边界可接续（start == until 允许）。
                for other in closed_rows:
                    if start < other["effective_until"] and other["effective_from"] < (end or FAR_FUTURE):
                        raise ConflictError("生效区间与已发布版本重叠")
                if open_row is not None:
                    # 已有开放版本时，新版本必须接续成为新的开放版本：
                    # 不允许有限窗口版本在开放区间内制造无法自动恢复的空档。
                    if end is not None:
                        raise ValidationError("当前已有开放生效版本，新版本必须省略 effective_until 以接续")
                    if start <= open_row["effective_from"]:
                        raise ConflictError("新生效日期必须严格晚于当前开放版本的生效日期")
                else:
                    last_until = max((o["effective_until"] for o in closed_rows), default=None)
                    if end is None and last_until is not None and start < last_until:
                        raise ConflictError("开放生效区间必须接续在最后版本之后")

                if open_row is not None:
                    # 自动接续：把原开放版本收口到新版本生效日。
                    connection.execute(
                        "UPDATE checklist_versions SET effective_until=? WHERE version_id=?",
                        (start, open_row["version_id"]),
                    )
                    append_event(connection, actor_id=actor_id,
                                 action="checklist_version.superseded",
                                 resource_type="checklist_version",
                                 resource_id=open_row["version_id"],
                                 detail={"effective_until": start, "by_version": version_id},
                                 occurred_at=self._now())
                connection.execute(
                    "UPDATE checklist_versions SET status=?, effective_from=?, effective_until=?, "
                    "published_at=? WHERE version_id=?",
                    (VERSION_PUBLISHED, start, end, self._now(), version_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.published",
                             resource_type="checklist_version", resource_id=version_id,
                             detail={"version_number": version["version_number"],
                                     "effective_from": start, "effective_until": end},
                             occurred_at=self._now())
                return "checklist_version", version_id, {"version_id": version_id,
                                                          "effective_from": start,
                                                          "effective_until": end}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_checklist_version", payload=payload, create=create)

    def rollback_version(self, *, request_id: str, actor_id: str,
                         version_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            version = self._version_row(connection, version_id)
            template = self._template_row(connection, version["template_id"])
            self._check_org(actor, template["organization_id"])
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                current = self._version_row(connection, version_id)
                if current["status"] == VERSION_ROLLED_BACK:
                    raise ConflictError("版本已经回滚，重复回滚不会生效")
                if current["status"] != VERSION_PUBLISHED:
                    raise ConflictError("只有已发布版本可以回滚")
                connection.execute(
                    "UPDATE checklist_versions SET status=?, rolled_back_by=?, rolled_back_at=?, "
                    "rollback_reason=? WHERE version_id=?",
                    (VERSION_ROLLED_BACK, actor_id, self._now(), reason, version_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.rolled_back",
                             resource_type="checklist_version", resource_id=version_id,
                             detail={"version_number": version["version_number"],
                                     "effective_from": version["effective_from"],
                                     "effective_until": version["effective_until"],
                                     "reason": reason}, occurred_at=self._now())
                return "checklist_version", version_id, {"version_id": version_id,
                                                          "status": VERSION_ROLLED_BACK}

            return self._idempotent(connection, request_id=request_id,
                                    action="rollback_checklist_version", payload=payload, create=create)

    def list_versions(self, template_id: str) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM checklist_versions WHERE template_id=? ORDER BY version_number",
                (template_id,),
            ).fetchall()
            return [self._version_summary(row) for row in rows]

    def get_version(self, version_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM checklist_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("清单版本不存在")
            result = self._version_summary(row)
            result["items"] = [item.to_dict() for item in self._version_from_row(row).items]
            return result

    def _version_summary(self, row) -> dict[str, Any]:
        return {
            "version_id": row["version_id"], "template_id": row["template_id"],
            "version_number": row["version_number"], "status": row["status"], "name": row["name"],
            "items_hash": row["items_hash"], "effective_from": row["effective_from"],
            "effective_until": row["effective_until"], "created_by": row["created_by"],
            "created_at": row["created_at"], "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"], "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"], "review_decision": row["review_decision"],
            "review_comment": row["review_comment"], "published_at": row["published_at"],
            "rolled_back_by": row["rolled_back_by"], "rolled_back_at": row["rolled_back_at"],
            "rollback_reason": row["rollback_reason"],
        }

    # ------------------------------------------------------------------ 企业覆盖层

    def _known_item_codes(self, connection, template_id: str, site_id: str) -> set[str]:
        # 以所有已发布版本的条目码并集作为合法引用范围（覆盖层可能跨越版本切换）；
        # 草稿不参与，避免未定稿条目影响覆盖层校验。
        codes: set[str] = set()
        for row in connection.execute(
            "SELECT items_json FROM checklist_versions WHERE template_id=? AND status=?",
            (template_id, VERSION_PUBLISHED),
        ):
            codes.update(item["code"] for item in json.loads(row["items_json"]))
        for row in connection.execute(
            "SELECT changes_json FROM checklist_overlays WHERE template_id=? AND site_id=? AND status=?",
            (template_id, site_id, OVERLAY_ACTIVE),
        ):
            for change in json.loads(row["changes_json"]):
                if change["kind"] == "add":
                    codes.add(change["code"])
        return codes

    def create_overlay(self, *, request_id: str, actor_id: str, template_id: str, site_id: str,
                       reason: str, effective_from: str, expires_at: str,
                       changes: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "template_id": template_id, "site_id": site_id,
                   "reason": reason, "effective_from": effective_from, "expires_at": expires_at,
                   "changes": changes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            template = self._template_row(connection, template_id)
            site = self._site(connection, site_id)
            self._check_org(actor, template["organization_id"])
            if site["organization_id"] != template["organization_id"]:
                raise NotFoundError("场所不属于模板所属组织")
            reason = self._text(reason, "reason")
            start = self._date(effective_from, "effective_from")
            end = self._date(expires_at, "expires_at")
            if not start < end:
                raise ValidationError("expires_at 必须晚于 effective_from")
            parsed_changes = self._changes_from_payload(changes)
            changes_data = [change.to_dict() for change in parsed_changes]

            def create() -> tuple[str, str, dict[str, Any]]:
                # 时钟与引用集合随时间变化，放入首次创建守卫，避免同号重试误判。
                if start < self._today():
                    raise ValidationError("覆盖层生效日期不能早于今天")
                known_codes = self._known_item_codes(connection, template_id, site_id)
                for change in parsed_changes:
                    if change.kind in {"remove", "modify"} and change.code not in known_codes:
                        raise ValidationError(f"覆盖目标条目不存在：{change.code}")
                    if change.kind == "add" and change.item is not None:
                        if not change.item.applicable_processes and not change.item.risk_conditions:
                            raise ValidationError("企业新增条目必须说明工艺或风险条件")
                revision_row = connection.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 AS next FROM checklist_overlays "
                    "WHERE template_id=? AND site_id=?",
                    (template_id, site_id),
                ).fetchone()
                revision = revision_row["next"]
                overlay_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO checklist_overlays(overlay_id,template_id,site_id,revision,reason,"
                    "effective_from,expires_at,status,changes_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (overlay_id, template_id, site_id, revision, reason, start, end,
                     OVERLAY_ACTIVE, canonical_json(changes_data), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="checklist_overlay.created",
                             resource_type="checklist_overlay", resource_id=overlay_id,
                             detail={"template_id": template_id, "site_id": site_id,
                                     "revision": revision, "effective_from": start,
                                     "expires_at": end, "change_count": len(changes_data)},
                             occurred_at=self._now())
                return "checklist_overlay", overlay_id, {"overlay_id": overlay_id,
                                                          "revision": revision}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_checklist_overlay", payload=payload, create=create)

    def revoke_overlay(self, *, request_id: str, actor_id: str,
                       overlay_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "overlay_id": overlay_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            row = connection.execute(
                "SELECT * FROM checklist_overlays WHERE overlay_id=?", (overlay_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("覆盖层不存在")
            template = self._template_row(connection, row["template_id"])
            self._check_org(actor, template["organization_id"])
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                current = connection.execute(
                    "SELECT * FROM checklist_overlays WHERE overlay_id=?", (overlay_id,)
                ).fetchone()
                if current["status"] == OVERLAY_REVOKED:
                    raise ConflictError("覆盖层已经撤销，重复撤销不会生效")
                connection.execute(
                    "UPDATE checklist_overlays SET status=?, revoked_by=?, revoked_at=?, "
                    "revoke_reason=? WHERE overlay_id=?",
                    (OVERLAY_REVOKED, actor_id, self._now(), reason, overlay_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_overlay.revoked",
                             resource_type="checklist_overlay", resource_id=overlay_id,
                             detail={"site_id": row["site_id"], "revision": row["revision"],
                                     "reason": reason}, occurred_at=self._now())
                return "checklist_overlay", overlay_id, {"overlay_id": overlay_id,
                                                          "status": OVERLAY_REVOKED}

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_checklist_overlay", payload=payload, create=create)

    def list_overlays(self, template_id: str, site_id: str) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM checklist_overlays WHERE template_id=? AND site_id=? ORDER BY revision",
                (template_id, site_id),
            ).fetchall()
            return [self._overlay_summary(row) for row in rows]

    def _overlay_summary(self, row) -> dict[str, Any]:
        return {
            "overlay_id": row["overlay_id"], "template_id": row["template_id"],
            "site_id": row["site_id"], "revision": row["revision"], "reason": row["reason"],
            "effective_from": row["effective_from"], "expires_at": row["expires_at"],
            "status": row["status"], "changes": json.loads(row["changes_json"]),
            "created_by": row["created_by"], "created_at": row["created_at"],
            "revoked_by": row["revoked_by"], "revoked_at": row["revoked_at"],
            "revoke_reason": row["revoke_reason"],
        }

    # ------------------------------------------------------------------ 解析

    def _site_tags(self, connection, site_id: str) -> tuple[frozenset[str], frozenset[str], dict[str, Any]]:
        processes: set[str] = set()
        risks: set[str] = set()
        snapshot: dict[str, Any] = {"process_profile": [], "pollution_factor": [], "enterprise_tag": []}
        rows = connection.execute(
            "SELECT category, external_key, payload_hash FROM domain_records "
            "WHERE site_id=? AND category IN ('process_profile','pollution_factor','enterprise_tag') "
            "ORDER BY category, external_key",
            (site_id,),
        ).fetchall()
        for row in rows:
            snapshot[row["category"]].append({"external_key": row["external_key"],
                                              "payload_hash": row["payload_hash"]})
            if row["category"] == "process_profile":
                processes.add(row["external_key"])
            else:
                risks.add(row["external_key"])
        return frozenset(processes), frozenset(risks), snapshot

    def _pick_version(self, connection, template_id: str, on_date: str):
        """选择某日适用的已发布版本。

        优先选择区间覆盖该日的版本；若因回滚后继版本造成空档，则回退到该日之前
        最近的未回滚版本（延续适用），并在来源中标注 fallback_after_rollback。
        """

        rows = connection.execute(
            "SELECT * FROM checklist_versions WHERE template_id=? AND status=? AND effective_from<=? "
            "ORDER BY effective_from DESC, version_number DESC",
            (template_id, VERSION_PUBLISHED, on_date),
        ).fetchall()
        if not rows:
            return None, False
        for row in rows:
            if row["effective_until"] is None or row["effective_until"] > on_date:
                return row, False
        # 空档：被回滚的后继版本留下缺口，延续最近的旧版本。
        return rows[0], True

    def _active_overlay_rows(self, connection, template_id: str, site_id: str, on_date: str):
        return connection.execute(
            "SELECT * FROM checklist_overlays WHERE template_id=? AND site_id=? AND status=? "
            "AND effective_from<=? AND expires_at>? "
            "ORDER BY effective_from, created_at, overlay_id",
            (template_id, site_id, OVERLAY_ACTIVE, on_date, on_date),
        ).fetchall()

    def _resolve(self, connection, template_id: str, site_id: str, on_date: str) -> dict[str, Any]:
        version_row, fallback = self._pick_version(connection, template_id, on_date)
        if version_row is None:
            raise ConflictError(f"{on_date} 没有可适用的已发布清单版本")
        version = self._version_from_row(version_row)
        processes, risks, tag_snapshot = self._site_tags(connection, site_id)
        overlay_rows = self._active_overlay_rows(connection, template_id, site_id, on_date)
        overlays = [(row["overlay_id"], row["revision"],
                     tuple(change_from_dict(d) for d in json.loads(row["changes_json"])))
                    for row in overlay_rows]
        resolved = resolve_checklist(version_id=version.version_id, items=version.items,
                                     processes=processes, risks=risks, overlays=overlays)
        if not resolved:
            raise ConflictError("适用性与覆盖层叠加后清单为空，不能使用")
        return {
            "version": version, "version_row": version_row, "fallback": fallback,
            "tag_snapshot": tag_snapshot, "processes": sorted(processes), "risks": sorted(risks),
            "overlays": [self._overlay_summary(row) for row in overlay_rows],
            "items": resolved,
        }

    def get_current_checklist(self, site_id: str, on_date: str | None = None) -> dict[str, Any]:
        """回答企业当前（或指定日期）适用的完整清单。"""

        on_date = self._date(on_date, "on_date") if on_date else self._today()
        with self.database.read() as connection:
            site = self._site(connection, site_id)
            template = self._template_for_org(connection, site["organization_id"])
            if template is None:
                raise NotFoundError("该组织尚未建立清单模板")
            resolved = self._resolve(connection, template["template_id"], site_id, on_date)
            version: ChecklistVersion = resolved["version"]
            return {
                "site_id": site_id, "on_date": on_date,
                "template_id": template["template_id"],
                "version": {"version_id": version.version_id,
                            "version_number": version.version_number,
                            "status": version.status,
                            "effective_from": version.effective_from,
                            "effective_until": version.effective_until,
                            "fallback_after_rollback": resolved["fallback"]},
                "enterprise_tags": {"processes": resolved["processes"], "risks": resolved["risks"]},
                "active_overlays": [{"overlay_id": o["overlay_id"], "revision": o["revision"],
                                     "effective_from": o["effective_from"],
                                     "expires_at": o["expires_at"], "reason": o["reason"]}
                                    for o in resolved["overlays"]],
                "items": [item.to_dict() for item in resolved["items"]],
            }

    def get_future_changes(self, site_id: str, from_date: str | None = None) -> dict[str, Any]:
        """回答今天之后清单将如何变化：版本切换与覆盖层起止。"""

        start = self._date(from_date, "from_date") if from_date else self._today()
        events: list[dict[str, Any]] = []
        with self.database.read() as connection:
            site = self._site(connection, site_id)
            template = self._template_for_org(connection, site["organization_id"])
            if template is None:
                raise NotFoundError("该组织尚未建立清单模板")
            template_id = template["template_id"]
            version_rows = connection.execute(
                "SELECT * FROM checklist_versions WHERE template_id=? ORDER BY effective_from",
                (template_id,),
            ).fetchall()
            for row in version_rows:
                if row["status"] not in {VERSION_PUBLISHED, VERSION_ROLLED_BACK}:
                    continue
                if row["effective_from"] and row["effective_from"] > start:
                    events.append({
                        "type": "version_effective", "effective_at": row["effective_from"],
                        "version_id": row["version_id"], "version_number": row["version_number"],
                        "status": row["status"],
                        "effective_until": row["effective_until"],
                        "note": "已回滚，不会适用" if row["status"] == VERSION_ROLLED_BACK else None,
                    })
                # 已回滚版本未真正沿区间生效，不产生到期事件。
                if (row["status"] == VERSION_PUBLISHED and row["effective_until"]
                        and row["effective_until"] > start):
                    events.append({
                        "type": "version_expiry", "effective_at": row["effective_until"],
                        "version_id": row["version_id"], "version_number": row["version_number"],
                        "status": row["status"],
                    })
            overlay_rows = connection.execute(
                "SELECT * FROM checklist_overlays WHERE template_id=? AND site_id=? "
                "ORDER BY effective_from, revision",
                (template_id, site_id),
            ).fetchall()
            for row in overlay_rows:
                if row["effective_from"] > start:
                    events.append({
                        "type": "overlay_effective", "effective_at": row["effective_from"],
                        "overlay_id": row["overlay_id"], "revision": row["revision"],
                        "status": row["status"],
                        "change_codes": [c["code"] for c in json.loads(row["changes_json"])],
                        "note": "已撤销，不会适用" if row["status"] == OVERLAY_REVOKED else None,
                    })
                if row["expires_at"] > start and row["status"] != OVERLAY_REVOKED:
                    events.append({
                        "type": "overlay_expiry", "effective_at": row["expires_at"],
                        "overlay_id": row["overlay_id"], "revision": row["revision"],
                        "status": row["status"],
                    })
        events.sort(key=lambda e: (e["effective_at"], e["type"],
                                   e.get("version_id") or e.get("overlay_id") or ""))
        return {"site_id": site_id, "from_date": start, "events": events}

    # ------------------------------------------------------------------ 每日任务

    def create_daily_task(self, *, request_id: str, actor_id: str, site_id: str,
                          task_date: str | None = None) -> WriteReceipt:
        task_date = self._date(task_date, "task_date") if task_date else self._today()
        payload = {"actor_id": actor_id, "site_id": site_id, "task_date": task_date}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._check_org(actor, site["organization_id"])
            template = self._template_for_org(connection, site["organization_id"])
            if template is None:
                raise NotFoundError("该组织尚未建立清单模板")

            def create() -> tuple[str, str, dict[str, Any]]:
                # 重复检测与清单解析都在首次创建时执行；同 request_id 重试直接回放，
                # 不会因为企业标签或版本随后变化而重新解析出不同内容。
                duplicate = connection.execute(
                    "SELECT task_id FROM inspection_tasks WHERE site_id=? AND task_date=?",
                    (site_id, task_date),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("该场所当日巡查任务已经生成，清单已冻结")
                resolved = self._resolve(connection, template["template_id"], site_id, task_date)
                version: ChecklistVersion = resolved["version"]
                frozen_items = [item.to_dict() for item in resolved["items"]]
                manifest = {
                    "site_id": site_id, "task_date": task_date,
                    "template_id": template["template_id"],
                    "version": {"version_id": version.version_id,
                                "version_number": version.version_number,
                                "effective_from": version.effective_from,
                                "effective_until": version.effective_until,
                                "fallback_after_rollback": resolved["fallback"]},
                    "enterprise_tag_snapshot": resolved["tag_snapshot"],
                    "active_overlay_snapshot": resolved["overlays"],
                    "resolved_at": self._now(),
                    "items": frozen_items,
                }
                manifest_hash = digest(manifest)
                task_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO inspection_tasks(task_id,site_id,task_date,status,template_id,"
                    "version_id,manifest_json,manifest_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (task_id, site_id, task_date, TASK_PENDING, template["template_id"],
                     version.version_id, canonical_json(manifest), manifest_hash,
                     actor_id, self._now()),
                )
                for position, item in enumerate(frozen_items):
                    connection.execute(
                        "INSERT INTO inspection_task_items(task_id,position,code,item_json,source_json) "
                        "VALUES(?,?,?,?,?)",
                        (task_id, position, item["code"], canonical_json(item),
                         canonical_json(item["sources"])),
                    )
                append_event(connection, actor_id=actor_id, action="inspection_task.created",
                             resource_type="inspection_task", resource_id=task_id,
                             detail={"site_id": site_id, "task_date": task_date,
                                     "version_id": version.version_id,
                                     "manifest_hash": manifest_hash,
                                     "item_count": len(frozen_items)},
                             occurred_at=self._now())
                return "inspection_task", task_id, {"task_id": task_id, "manifest_hash": manifest_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_daily_task", payload=payload, create=create)

    def _task_row(self, connection, task_id: str):
        row = connection.execute(
            "SELECT * FROM inspection_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("巡查任务不存在")
        return row

    def _task_detail(self, connection, row, *, include_manifest: bool) -> dict[str, Any]:
        item_rows = connection.execute(
            "SELECT * FROM inspection_task_items WHERE task_id=? ORDER BY position", (row["task_id"],)
        ).fetchall()
        items = [{"position": r["position"], "code": r["code"],
                  "item": json.loads(r["item_json"]), "sources": json.loads(r["source_json"])}
                 for r in item_rows]
        result = {
            "task_id": row["task_id"], "site_id": row["site_id"], "task_date": row["task_date"],
            "status": row["status"], "template_id": row["template_id"],
            "version_id": row["version_id"], "manifest_hash": row["manifest_hash"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "started_at": row["started_at"], "completed_at": row["completed_at"],
            "items": items,
        }
        if include_manifest:
            result["manifest"] = json.loads(row["manifest_json"])
        return result

    def start_task(self, *, actor_id: str, task_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = self._task_row(connection, task_id)
            if row["status"] in {TASK_PENDING}:
                connection.execute(
                    "UPDATE inspection_tasks SET status=?, started_at=? WHERE task_id=?",
                    (TASK_STARTED, self._now(), task_id),
                )
                append_event(connection, actor_id=actor_id, action="inspection_task.started",
                             resource_type="inspection_task", resource_id=task_id,
                             detail={}, occurred_at=self._now())
                status = TASK_STARTED
            else:
                # 已开始/完成：确定的重复调用，直接回当前状态。
                status = row["status"]
            return {"task_id": task_id, "status": status}

    def complete_task(self, *, actor_id: str, task_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = self._task_row(connection, task_id)
            if row["status"] == TASK_PENDING:
                raise ConflictError("任务尚未开始，不能完成")
            if row["status"] == TASK_COMPLETED:
                return {"task_id": task_id, "status": TASK_COMPLETED}
            connection.execute(
                "UPDATE inspection_tasks SET status=?, completed_at=? WHERE task_id=?",
                (TASK_COMPLETED, self._now(), task_id),
            )
            append_event(connection, actor_id=actor_id, action="inspection_task.completed",
                         resource_type="inspection_task", resource_id=task_id,
                         detail={}, occurred_at=self._now())
            return {"task_id": task_id, "status": TASK_COMPLETED}

    def get_task(self, task_id: str) -> dict[str, Any]:
        """回答任意历史任务的冻结条目与完整来源（含标签快照）。"""

        with self.database.read() as connection:
            return self._task_detail(connection, self._task_row(connection, task_id),
                                     include_manifest=True)

    def get_task_rule_sources(self, task_id: str) -> dict[str, Any]:
        """只聚焦规则来源：每条冻结条目来自哪个版本/覆盖层。"""

        with self.database.read() as connection:
            row = self._task_row(connection, task_id)
            detail = self._task_detail(connection, row, include_manifest=False)
            manifest = json.loads(row["manifest_json"])
            return {
                "task_id": task_id, "site_id": row["site_id"], "task_date": row["task_date"],
                "version": manifest["version"],
                "enterprise_tag_snapshot": manifest["enterprise_tag_snapshot"],
                "active_overlay_snapshot": [
                    {"overlay_id": o["overlay_id"], "revision": o["revision"],
                     "effective_from": o["effective_from"], "expires_at": o["expires_at"],
                     "status": o["status"], "reason": o["reason"]}
                    for o in manifest["active_overlay_snapshot"]
                ],
                "manifest_hash": row["manifest_hash"],
                "items": [{"position": i["position"], "code": i["code"], "sources": i["sources"]}
                          for i in detail["items"]],
            }

    def list_tasks(self, site_id: str, task_date: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM inspection_tasks WHERE site_id=?"
        parameters: list[Any] = [site_id]
        if task_date:
            query += " AND task_date=?"
            parameters.append(self._date(task_date, "task_date"))
        query += " ORDER BY task_date"
        with self.database.read() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [{k: row[k] for k in ("task_id", "task_date", "status", "version_id",
                                    "manifest_hash", "created_at", "started_at", "completed_at")}
                for row in rows]
