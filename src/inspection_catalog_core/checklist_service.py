"""实现专属巡查清单的模板版本、企业覆盖、任务冻结与规则溯源。

关键不变量：

* 所有写操作在 ``BEGIN IMMEDIATE`` 事务内完成并先做状态守卫，
  因此并发的提交/复核/发布中至多一方成功，另一方得到确定的冲突；
* 任务生成时把最终条目与来源逐行冻结到 ``inspection_task_items``，
  此后版本发布、回滚（撤销）、覆盖到期或企业标签变化都不会改写历史任务；
* 撤销只改变版本/覆盖状态，只影响撤销之后生成的任务。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .checklist import applicability_matches, half_open_overlaps, resolve_checklist
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    Actor,
    Applicability,
    ChecklistItem,
    EnterpriseOverride,
    FrozenTaskItem,
    InspectionTask,
    TemplateVersion,
    WriteReceipt,
)
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
ITEM_CATEGORIES = frozenset({"waste_gas", "dust_collection", "hazardous_waste", "other"})
MAX_OVERRIDE_DAYS = 366


def parse_endpoint(value: str, field: str) -> str:
    """把日期或带时区的时间统一为 UTC ISO-8601（``Z`` 结尾）。"""

    text = str(value).strip()
    try:
        if len(text) == 10:
            moment = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                raise ValueError("缺少时区")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 或带时区的 ISO-8601 时间") from exc
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ChecklistService:
    """提供清单模板、企业覆盖、每日任务和规则溯源能力。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础校验
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _reason(self, value: str | None, field: str = "reason") -> str:
        return self._text(value or "", field, 500)

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

    def _update_one(self, connection, sql: str, parameters: tuple) -> None:
        """执行条件 UPDATE，并在状态守卫未命中任何行时给出确定冲突。"""

        cursor = connection.execute(sql, parameters)
        if cursor.rowcount == 0:
            raise ConflictError("对象状态已变化，请刷新后重试")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _site_scope(self, connection, actor: Actor, site_id: str):
        site = self._site(connection, site_id)
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return site

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        result = create()
        # create 可返回第四个布尔值，表示业务结果是对既有资源的回放（如重复日期任务）。
        force_replay = False
        if len(result) == 4:
            resource_type, resource_id, response, force_replay = result
        else:
            resource_type, resource_id, response = result
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, bool(force_replay))

    def _applicability(self, value: Any) -> Applicability:
        if not isinstance(value, dict):
            raise ValidationError("applicability 必须是对象")
        profiles = value.get("process_profiles")
        tags = value.get("enterprise_tags", [])
        if not isinstance(profiles, list) or not profiles:
            raise ValidationError("process_profiles 必须是非空数组")
        if not isinstance(tags, list):
            raise ValidationError("enterprise_tags 必须是数组")
        profile_set = frozenset(self._identifier(item, "process_profile") for item in profiles)
        tag_set = frozenset(self._identifier(item, "enterprise_tag") for item in tags)
        if len(profile_set) != len(profiles) or len(tag_set) != len(tags):
            raise ValidationError("适用条件不能包含重复值")
        return Applicability(profile_set, tag_set)

    def _items(self, value: Any) -> tuple[ChecklistItem, ...]:
        if not isinstance(value, list) or not value:
            raise ValidationError("items 必须是非空数组")
        if len(value) > 200:
            raise ValidationError("items 不能超过 200 条")
        items: list[ChecklistItem] = []
        seen: set[str] = set()
        for entry in value:
            if not isinstance(entry, dict):
                raise ValidationError("清单条目必须是对象")
            code = self._identifier(entry.get("code", ""), "item.code")
            category = self._text(entry.get("category", ""), "item.category", 64)
            if category not in ITEM_CATEGORIES:
                raise ValidationError(
                    "item.category 只能是 waste_gas、dust_collection、hazardous_waste、other")
            content = self._text(entry.get("content", ""), "item.content", 1000)
            if code in seen:
                raise ValidationError(f"清单条目编码重复：{code}")
            seen.add(code)
            items.append(ChecklistItem(code, category, content))
        return tuple(items)

    # ------------------------------------------------------------------
    # 行映射
    # ------------------------------------------------------------------

    def _version_from_row(self, row) -> TemplateVersion:
        applicability = json.loads(row["applicability_json"])
        items_data = json.loads(row["items_json"])
        return TemplateVersion(
            version_id=row["version_id"],
            template_id=row["template_id"],
            version_no=row["version_no"],
            status=row["status"],
            applicability=Applicability(frozenset(applicability["process_profiles"]),
                                        frozenset(applicability["enterprise_tags"])),
            items=tuple(ChecklistItem(**item) for item in items_data),
            content_hash=row["content_hash"],
            drafted_by=row["drafted_by"],
            drafted_at=row["drafted_at"],
            submitted_by=row["submitted_by"],
            submitted_at=row["submitted_at"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_result=row["review_result"],
            review_reason=row["review_reason"],
            published_by=row["published_by"],
            published_at=row["published_at"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            revoked_by=row["revoked_by"],
            revoked_at=row["revoked_at"],
            revoke_reason=row["revoke_reason"],
        )

    def _load_version(self, connection, version_id: str) -> TemplateVersion:
        row = connection.execute(
            "SELECT * FROM checklist_template_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("模板版本不存在")
        return self._version_from_row(row)

    def _override_from_row(self, row) -> EnterpriseOverride:
        content_data = json.loads(row["content_json"]) if row["content_json"] else None
        content = ChecklistItem(**content_data) if content_data else None
        return EnterpriseOverride(
            override_id=row["override_id"],
            site_id=row["site_id"],
            template_id=row["template_id"],
            kind=row["kind"],
            item_code=row["item_code"],
            content=content,
            reason=row["reason"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            status=row["status"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            revoked_by=row["revoked_by"],
            revoked_at=row["revoked_at"],
            revoke_reason=row["revoke_reason"],
        )

    def _published_versions(self, connection) -> list[TemplateVersion]:
        rows = connection.execute(
            "SELECT * FROM checklist_template_versions WHERE status='published' "
            "ORDER BY template_id, version_no"
        ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def _site_overrides(self, connection, site_id: str) -> list[EnterpriseOverride]:
        rows = connection.execute(
            "SELECT * FROM enterprise_overrides WHERE site_id=? ORDER BY created_at, override_id",
            (site_id,),
        ).fetchall()
        return [self._override_from_row(row) for row in rows]

    def _site_labels(self, connection, site_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        rows = connection.execute(
            "SELECT category, external_key, payload_json FROM domain_records "
            "WHERE site_id=? AND category IN ('process_profile','enterprise_tag')",
            (site_id,),
        ).fetchall()
        profiles: list[str] = []
        tags: list[str] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("enabled") is False:
                continue
            (profiles if row["category"] == "process_profile" else tags).append(row["external_key"])
        return tuple(sorted(profiles)), tuple(sorted(tags))

    # ------------------------------------------------------------------
    # 模板与版本起草
    # ------------------------------------------------------------------

    def create_template(self, *, request_id: str, actor_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                template_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO checklist_templates(template_id,name,version_count,created_by,created_at) "
                    "VALUES(?,?,0,?,?)",
                    (template_id, name, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="checklist_template.created",
                             resource_type="checklist_template", resource_id=template_id,
                             detail={"name": name}, occurred_at=self._now())
                return "checklist_template", template_id, {"template_id": template_id, "name": name}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_checklist_template", payload=payload, create=create)

    def create_template_version(self, *, request_id: str, actor_id: str, template_id: str,
                                applicability: dict[str, Any],
                                items: list[dict[str, Any]]) -> WriteReceipt:
        applicability_obj = self._applicability(applicability)
        items_obj = self._items(items)
        payload = {"actor_id": actor_id, "template_id": template_id,
                   "applicability": applicability, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            template_id = self._identifier(template_id, "template_id")
            template = connection.execute(
                "SELECT * FROM checklist_templates WHERE template_id=?", (template_id,)
            ).fetchone()
            if template is None:
                raise NotFoundError("清单模板不存在")
            applicability_norm = {
                "process_profiles": sorted(applicability_obj.process_profiles),
                "enterprise_tags": sorted(applicability_obj.enterprise_tags),
            }
            items_norm = [item.__dict__ for item in items_obj]
            content_hash = digest({"applicability": applicability_norm, "items": items_norm})

            def create() -> tuple[str, str, dict[str, Any]]:
                version_no = template["version_count"] + 1
                connection.execute(
                    "UPDATE checklist_templates SET version_count=? WHERE template_id=?",
                    (version_no, template_id),
                )
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO checklist_template_versions(version_id,template_id,version_no,status,"
                    "applicability_json,items_json,content_hash,drafted_by,drafted_at) "
                    "VALUES(?,?,?,'draft',?,?,?,?,?)",
                    (version_id, template_id, version_no, canonical_json(applicability_norm),
                     canonical_json(items_norm), content_hash, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.drafted",
                             resource_type="checklist_template_version", resource_id=version_id,
                             detail={"template_id": template_id, "version_no": version_no,
                                     "content_hash": content_hash},
                             occurred_at=self._now())
                return ("checklist_template_version", version_id,
                        {"version_id": version_id, "template_id": template_id,
                         "version_no": version_no, "status": "draft"})

            return self._idempotent(connection, request_id=request_id,
                                    action="create_checklist_version", payload=payload, create=create)

    def update_template_version(self, *, request_id: str, actor_id: str, version_id: str,
                                applicability: dict[str, Any],
                                items: list[dict[str, Any]]) -> WriteReceipt:
        applicability_obj = self._applicability(applicability)
        items_obj = self._items(items)
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "applicability": applicability, "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version_id = self._identifier(version_id, "version_id")
            current = self._load_version(connection, version_id)
            if current.status != "draft":
                raise ConflictError("只有草稿状态的版本可以修改")
            if current.drafted_by != actor_id and actor.role != "admin":
                raise PermissionDenied("只能修改本人起草的版本")
            applicability_norm = {
                "process_profiles": sorted(applicability_obj.process_profiles),
                "enterprise_tags": sorted(applicability_obj.enterprise_tags),
            }
            items_norm = [item.__dict__ for item in items_obj]
            content_hash = digest({"applicability": applicability_norm, "items": items_norm})

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE checklist_template_versions SET applicability_json=?, items_json=?, "
                    "content_hash=? WHERE version_id=?",
                    (canonical_json(applicability_norm), canonical_json(items_norm),
                     content_hash, version_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.updated",
                             resource_type="checklist_template_version", resource_id=version_id,
                             detail={"content_hash": content_hash}, occurred_at=self._now())
                return ("checklist_template_version", version_id,
                        {"version_id": version_id, "status": "draft", "content_hash": content_hash})

            return self._idempotent(connection, request_id=request_id,
                                    action="update_checklist_version", payload=payload, create=create)

    def submit_version(self, *, request_id: str, actor_id: str, version_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            version_id = self._identifier(version_id, "version_id")
            current = self._load_version(connection, version_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if current.status == "submitted":
                    raise ConflictError("版本已经提交，等待复核")
                if current.status != "draft":
                    raise ConflictError(f"版本处于 {current.status} 状态，不能提交复核")
                self._update_one(
                    connection,
                    "UPDATE checklist_template_versions SET status='submitted',submitted_by=?,"
                    "submitted_at=? WHERE version_id=? AND status='draft'",
                    (actor_id, self._now(), version_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.submitted",
                             resource_type="checklist_template_version", resource_id=version_id,
                             detail={"template_id": current.template_id,
                                     "version_no": current.version_no},
                             occurred_at=self._now())
                return ("checklist_template_version", version_id,
                        {"version_id": version_id, "status": "submitted"})

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_checklist_version", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 复核与发布
    # ------------------------------------------------------------------

    def review_version(self, *, request_id: str, actor_id: str, version_id: str,
                       result: str, reason: str | None = None) -> WriteReceipt:
        if result not in ("approved", "rejected"):
            raise ValidationError("result 只能是 approved 或 rejected")
        reason_text = self._reason(reason) if result == "rejected" else (
            str(reason).strip() if reason else None)
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "result": result, "reason": reason_text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            version_id = self._identifier(version_id, "version_id")
            current = self._load_version(connection, version_id)
            if current.submitted_by == actor_id:
                raise PermissionDenied("提交人不能复核自己提交的版本")

            def create() -> tuple[str, str, dict[str, Any]]:
                if current.status not in ("submitted",):
                    raise ConflictError(f"版本处于 {current.status} 状态，不能复核")
                self._update_one(
                    connection,
                    "UPDATE checklist_template_versions SET status=?,reviewed_by=?,reviewed_at=?,"
                    "review_result=?,review_reason=? WHERE version_id=? AND status='submitted'",
                    (result, actor_id, self._now(), result, reason_text, version_id),
                )
                append_event(connection, actor_id=actor_id,
                             action=f"checklist_version.review_{result}",
                             resource_type="checklist_template_version", resource_id=version_id,
                             detail={"template_id": current.template_id,
                                     "version_no": current.version_no, "reason": reason_text},
                             occurred_at=self._now())
                return ("checklist_template_version", version_id,
                        {"version_id": version_id, "status": result})

            return self._idempotent(connection, request_id=request_id,
                                    action="review_checklist_version", payload=payload, create=create)

    def publish_version(self, *, request_id: str, actor_id: str, version_id: str,
                        effective_from: str, effective_to: str | None = None) -> WriteReceipt:
        start = parse_endpoint(effective_from, "effective_from")
        end = parse_endpoint(effective_to, "effective_to") if effective_to else None
        if end is not None and end <= start:
            raise ValidationError("生效区间必须满足 effective_from < effective_to")
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "effective_from": start, "effective_to": end}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            version_id = self._identifier(version_id, "version_id")
            current = self._load_version(connection, version_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if current.status == "published":
                    raise ConflictError("版本已经发布，不能重复发布")
                if current.status != "approved":
                    raise ConflictError("只有复核通过的版本才能发布")
                siblings = connection.execute(
                    "SELECT * FROM checklist_template_versions WHERE template_id=? AND status='published'",
                    (current.template_id,),
                ).fetchall()
                for sibling in siblings:
                    if half_open_overlaps(start, end,
                                          sibling["effective_from"], sibling["effective_to"]):
                        raise ConflictError(
                            f"与已发布版本 {sibling['version_no']} 的生效区间重叠")
                self._update_one(
                    connection,
                    "UPDATE checklist_template_versions SET status='published',published_by=?,"
                    "published_at=?,effective_from=?,effective_to=? "
                    "WHERE version_id=? AND status='approved'",
                    (actor_id, self._now(), start, end, version_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.published",
                             resource_type="checklist_template_version", resource_id=version_id,
                             detail={"template_id": current.template_id,
                                     "version_no": current.version_no,
                                     "effective_from": start, "effective_to": end},
                             occurred_at=self._now())
                return ("checklist_template_version", version_id,
                        {"version_id": version_id, "status": "published",
                         "effective_from": start, "effective_to": end})

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_checklist_version", payload=payload, create=create)

    def revoke_version(self, *, request_id: str, actor_id: str, version_id: str,
                       reason: str) -> WriteReceipt:
        reason_text = self._reason(reason)
        payload = {"actor_id": actor_id, "version_id": version_id, "reason": reason_text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            version_id = self._identifier(version_id, "version_id")
            current = self._load_version(connection, version_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if current.status == "revoked":
                    raise ConflictError("版本已经撤销")
                if current.status != "published":
                    raise ConflictError("只能撤销已发布的版本")
                self._update_one(
                    connection,
                    "UPDATE checklist_template_versions SET status='revoked',revoked_by=?,"
                    "revoked_at=?,revoke_reason=? WHERE version_id=? AND status='published'",
                    (actor_id, self._now(), reason_text, version_id),
                )
                append_event(connection, actor_id=actor_id, action="checklist_version.revoked",
                             resource_type="checklist_template_version", resource_id=version_id,
                             detail={"template_id": current.template_id,
                                     "version_no": current.version_no, "reason": reason_text},
                             occurred_at=self._now())
                # 撤销只影响尚未生成的任务：已冻结任务不做任何修改。
                return ("checklist_template_version", version_id,
                        {"version_id": version_id, "status": "revoked"})

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_checklist_version", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 企业覆盖层
    # ------------------------------------------------------------------

    def add_override(self, *, request_id: str, actor_id: str, site_id: str, kind: str,
                     item_code: str, reason: str, valid_from: str, valid_to: str,
                     template_id: str | None = None,
                     content: dict[str, Any] | None = None) -> WriteReceipt:
        if kind not in ("add", "remove"):
            raise ValidationError("kind 只能是 add 或 remove")
        item_code = self._identifier(item_code, "item_code")
        reason_text = self._reason(reason)
        start = parse_endpoint(valid_from, "valid_from")
        end = parse_endpoint(valid_to, "valid_to")
        if end <= start:
            raise ValidationError("覆盖期限必须满足 valid_from < valid_to")
        duration = datetime.fromisoformat(end.replace("Z", "+00:00")) - \
            datetime.fromisoformat(start.replace("Z", "+00:00"))
        if duration > timedelta(days=MAX_OVERRIDE_DAYS):
            raise ValidationError(f"企业覆盖期限不能超过 {MAX_OVERRIDE_DAYS} 天")
        if template_id is not None:
            template_id = self._identifier(template_id, "template_id")
        content_obj: ChecklistItem | None = None
        if kind == "add":
            if not isinstance(content, dict):
                raise ValidationError("add 覆盖必须提供 content")
            category = self._text(content.get("category", ""), "content.category", 64)
            if category not in ITEM_CATEGORIES:
                raise ValidationError(
                    "content.category 只能是 waste_gas、dust_collection、hazardous_waste、other")
            text = self._text(content.get("content", ""), "content.content", 1000)
            content_obj = ChecklistItem(item_code, category, text)
        elif content is not None:
            raise ValidationError("remove 覆盖不能携带 content")
        payload = {"actor_id": actor_id, "site_id": site_id, "kind": kind,
                   "item_code": item_code, "reason": reason_text, "valid_from": start,
                   "valid_to": end, "template_id": template_id,
                   "content": content_obj.__dict__ if content_obj else None}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_scope(connection, actor, site_id)
            if template_id is not None and connection.execute(
                    "SELECT 1 FROM checklist_templates WHERE template_id=?",
                    (template_id,)).fetchone() is None:
                raise NotFoundError("清单模板不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                clashes = connection.execute(
                    "SELECT override_id FROM enterprise_overrides WHERE site_id=? AND kind=? "
                    "AND item_code=? AND status='active'",
                    (site_id, kind, item_code),
                ).fetchall()
                for clash in clashes:
                    existing = connection.execute(
                        "SELECT * FROM enterprise_overrides WHERE override_id=?",
                        (clash["override_id"],),
                    ).fetchone()
                    if (template_id == existing["template_id"]
                            and half_open_overlaps(start, end,
                                                   existing["valid_from"], existing["valid_to"])):
                        raise ConflictError("同一企业条目的有效覆盖区间不能重叠")
                override_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO enterprise_overrides(override_id,site_id,template_id,kind,"
                    "item_code,content_json,reason,valid_from,valid_to,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)",
                    (override_id, site_id, template_id, kind, item_code,
                     canonical_json(content_obj.__dict__) if content_obj else None,
                     reason_text, start, end, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id,
                             action=f"enterprise_override.added_{kind}",
                             resource_type="enterprise_override", resource_id=override_id,
                             detail={"site_id": site_id, "template_id": template_id,
                                     "item_code": item_code, "valid_from": start,
                                     "valid_to": end, "reason": reason_text},
                             occurred_at=self._now())
                return ("enterprise_override", override_id,
                        {"override_id": override_id, "status": "active"})

            return self._idempotent(connection, request_id=request_id,
                                    action="add_enterprise_override", payload=payload, create=create)

    def revoke_override(self, *, request_id: str, actor_id: str, override_id: str,
                        reason: str) -> WriteReceipt:
        reason_text = self._reason(reason)
        payload = {"actor_id": actor_id, "override_id": override_id, "reason": reason_text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            override_id = self._identifier(override_id, "override_id")
            row = connection.execute(
                "SELECT * FROM enterprise_overrides WHERE override_id=?", (override_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("企业覆盖不存在")
            override = self._override_from_row(row)
            self._site_scope(connection, actor, override.site_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if override.status == "revoked":
                    raise ConflictError("覆盖已经撤销")
                self._update_one(
                    connection,
                    "UPDATE enterprise_overrides SET status='revoked',revoked_by=?,"
                    "revoked_at=?,revoke_reason=? WHERE override_id=? AND status='active'",
                    (actor_id, self._now(), reason_text, override_id),
                )
                append_event(connection, actor_id=actor_id, action="enterprise_override.revoked",
                             resource_type="enterprise_override", resource_id=override_id,
                             detail={"site_id": override.site_id,
                                     "item_code": override.item_code, "reason": reason_text},
                             occurred_at=self._now())
                return ("enterprise_override", override_id,
                        {"override_id": override_id, "status": "revoked"})

            return self._idempotent(connection, request_id=request_id,
                                    action="revoke_enterprise_override", payload=payload,
                                    create=create)

    # ------------------------------------------------------------------
    # 每日任务：生成即冻结
    # ------------------------------------------------------------------

    def generate_daily_task(self, *, request_id: str, actor_id: str, site_id: str,
                            task_date: str, effective_at: str | None = None) -> WriteReceipt:
        try:
            day = date.fromisoformat(str(task_date))
        except (TypeError, ValueError) as exc:
            raise ValidationError("task_date 必须是 YYYY-MM-DD") from exc
        if day.isoformat() != str(task_date):
            raise ValidationError("task_date 必须是 YYYY-MM-DD")
        moment = parse_endpoint(effective_at, "effective_at") if effective_at else \
            f"{day.isoformat()}T00:00:00Z"
        payload = {"actor_id": actor_id, "site_id": site_id,
                   "task_date": day.isoformat(), "effective_at": moment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site_id = self._identifier(site_id, "site_id")
            self._site_scope(connection, actor, site_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                # 同一天重复创建是确定操作：回放既有任务，绝不重新解析快照。
                existing = connection.execute(
                    "SELECT task_id FROM inspection_tasks WHERE site_id=? AND task_date=?",
                    (site_id, day.isoformat()),
                ).fetchone()
                if existing is not None:
                    return ("inspection_task", existing["task_id"],
                            {"task_id": existing["task_id"], "duplicate_date": day.isoformat()},
                            True)
                profiles, tags = self._site_labels(connection, site_id)
                versions = self._published_versions(connection)
                overrides = self._site_overrides(connection, site_id)
                resolution = resolve_checklist(versions, overrides, profiles, tags, moment)
                task_id = uuid.uuid4().hex
                context = {
                    "effective_at": moment,
                    "process_profiles": list(resolution["process_profiles"]),
                    "enterprise_tags": list(resolution["enterprise_tags"]),
                    "version_ids": [v.version_id for v in resolution["matched_versions"]],
                    "override_ids": [o.override_id for o in resolution["active_overrides"]],
                    "removals": list(resolution["removals"]),
                }
                snapshot_hash = digest({"context": context, "items": [
                    item.__dict__ | {"sources": list(item.sources)}
                    for item in resolution["items"]
                ]})
                connection.execute(
                    "INSERT INTO inspection_tasks(task_id,site_id,task_date,status,"
                    "resolution_json,snapshot_hash,created_by,created_at) "
                    "VALUES(?,?,?,'generated',?,?,?,?)",
                    (task_id, site_id, day.isoformat(), canonical_json(context),
                     snapshot_hash, actor_id, self._now()),
                )
                for item in resolution["items"]:
                    connection.execute(
                        "INSERT INTO inspection_task_items(task_id,position,item_code,category,"
                        "content,sources_json) VALUES(?,?,?,?,?,?)",
                        (task_id, item.position, item.code, item.category, item.content,
                         canonical_json(list(item.sources))),
                    )
                append_event(connection, actor_id=actor_id, action="inspection_task.generated",
                             resource_type="inspection_task", resource_id=task_id,
                             detail={"site_id": site_id, "task_date": day.isoformat(),
                                     "item_count": len(resolution["items"]),
                                     "snapshot_hash": snapshot_hash},
                             occurred_at=self._now())
                return ("inspection_task", task_id,
                        {"task_id": task_id, "site_id": site_id,
                         "task_date": day.isoformat(), "status": "generated",
                         "item_count": len(resolution["items"]),
                         "snapshot_hash": snapshot_hash})

            return self._idempotent(connection, request_id=request_id,
                                    action="generate_daily_task", payload=payload, create=create)

    def _transition_task(self, connection, *, actor: Actor, task_id: str,
                         action: str, event: str, allowed: tuple[str, ...],
                         target: str) -> WriteReceipt:
        task = connection.execute(
            "SELECT * FROM inspection_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise NotFoundError("巡查任务不存在")
        site = self._site_scope(connection, actor, task["site_id"])
        if task["status"] not in allowed:
            raise ConflictError(f"任务处于 {task['status']} 状态，不能{action}")
        column = "started" if target == "started" else "completed" if target == "completed" else None
        placeholders = ",".join("?" for _ in allowed)
        if column:
            sql = (
                f"UPDATE inspection_tasks SET status=?,{column}_by=?,{column}_at=? "
                f"WHERE task_id=? AND status IN ({placeholders})"
            )
            parameters = (target, actor.actor_id, self._now(), task_id, *allowed)
        else:
            sql = (
                "UPDATE inspection_tasks SET status=? "
                f"WHERE task_id=? AND status IN ({placeholders})"
            )
            parameters = (target, task_id, *allowed)
        self._update_one(connection, sql, parameters)
        append_event(connection, actor_id=actor.actor_id, action=event,
                     resource_type="inspection_task", resource_id=task_id,
                     detail={"site_id": site["site_id"], "from_status": task["status"],
                             "to_status": target},
                     occurred_at=self._now())
        return WriteReceipt("__direct__", "inspection_task", task_id, False)

    def start_task(self, *, actor_id: str, task_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            task_id = self._identifier(task_id, "task_id")
            self._transition_task(connection, actor=actor, task_id=task_id,
                                  action="开始", event="inspection_task.started",
                                  allowed=("generated",), target="started")
            return {"task_id": task_id, "status": "started"}

    def complete_task(self, *, actor_id: str, task_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            task_id = self._identifier(task_id, "task_id")
            self._transition_task(connection, actor=actor, task_id=task_id,
                                  action="完成", event="inspection_task.completed",
                                  allowed=("started",), target="completed")
            return {"task_id": task_id, "status": "completed"}

    def cancel_task(self, *, request_id: str, actor_id: str, task_id: str,
                    reason: str) -> WriteReceipt:
        reason_text = self._reason(reason)
        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason_text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            task_id = self._identifier(task_id, "task_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = connection.execute(
                    "SELECT * FROM inspection_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if task is None:
                    raise NotFoundError("巡查任务不存在")
                self._site_scope(connection, actor, task["site_id"])
                if task["status"] != "generated":
                    raise ConflictError("只有尚未开始的任务可以取消")
                self._update_one(
                    connection,
                    "UPDATE inspection_tasks SET status='cancelled' WHERE task_id=? "
                    "AND status='generated'",
                    (task_id,),
                )
                append_event(connection, actor_id=actor_id, action="inspection_task.cancelled",
                             resource_type="inspection_task", resource_id=task_id,
                             detail={"reason": reason_text}, occurred_at=self._now())
                return ("inspection_task", task_id,
                        {"task_id": task_id, "status": "cancelled"})

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_inspection_task", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：当前清单、未来变更、历史任务溯源
    # ------------------------------------------------------------------

    def current_checklist(self, site_id: str, effective_at: str | None = None) -> dict[str, Any]:
        moment = parse_endpoint(effective_at, "effective_at") if effective_at else self._now()
        connection = self.database.connection
        site = self._site(connection, site_id)
        profiles, tags = self._site_labels(connection, site["site_id"])
        resolution = resolve_checklist(
            self._published_versions(connection),
            self._site_overrides(connection, site["site_id"]),
            profiles, tags, moment,
        )
        return {
            "site_id": site["site_id"],
            "effective_at": moment,
            "process_profiles": list(resolution["process_profiles"]),
            "enterprise_tags": list(resolution["enterprise_tags"]),
            "matched_versions": [self._version_summary(v) for v in resolution["matched_versions"]],
            "active_overrides": [self._override_summary(o) for o in resolution["active_overrides"]],
            "removals": list(resolution["removals"]),
            "items": [self._frozen_item_dict(item) for item in resolution["items"]],
        }

    def future_changes(self, site_id: str, within_to: str | None = None) -> dict[str, Any]:
        """回答“相对当前时刻，该企业清单未来会如何变化”。

        事件来源包括：版本开始生效、版本到达生效终点、覆盖开始与覆盖到期；
        版本撤销是立即操作，不产生未来事件。适用性按企业当前标签判断。
        """

        now_text = self._now()
        horizon = parse_endpoint(within_to, "within_to") if within_to else None
        connection = self.database.connection
        site = self._site(connection, site_id)
        profiles, tags = self._site_labels(connection, site["site_id"])
        events: list[dict[str, Any]] = []
        for version in self._published_versions(connection):
            if not applicability_matches(version.applicability, profiles, tags):
                continue
            if version.effective_from > now_text and (
                    horizon is None or version.effective_from < horizon):
                events.append({
                    "at": version.effective_from, "type": "version_effective",
                    "template_id": version.template_id, "version_no": version.version_no,
                    "version_id": version.version_id,
                })
            if version.effective_to and version.effective_to > now_text and (
                    horizon is None or version.effective_to < horizon):
                events.append({
                    "at": version.effective_to, "type": "version_expires",
                    "template_id": version.template_id, "version_no": version.version_no,
                    "version_id": version.version_id,
                })
        for override in self._site_overrides(connection, site["site_id"]):
            if override.status != "active":
                continue
            if override.valid_from > now_text and (
                    horizon is None or override.valid_from < horizon):
                events.append({
                    "at": override.valid_from, "type": "override_starts",
                    "override_id": override.override_id, "kind": override.kind,
                    "item_code": override.item_code,
                })
            if override.valid_to > now_text and (
                    horizon is None or override.valid_to < horizon):
                events.append({
                    "at": override.valid_to, "type": "override_expires",
                    "override_id": override.override_id, "kind": override.kind,
                    "item_code": override.item_code,
                })
        events.sort(key=lambda event: (event["at"], event["type"], event.get("version_id", ""),
                                       event.get("override_id", "")))
        return {"site_id": site["site_id"], "now": now_text, "items": events}

    def get_task(self, task_id: str) -> InspectionTask:
        row = self.database.connection.execute(
            "SELECT * FROM inspection_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("巡查任务不存在")
        context = json.loads(row["resolution_json"])
        item_rows = self.database.connection.execute(
            "SELECT * FROM inspection_task_items WHERE task_id=? ORDER BY position", (task_id,)
        ).fetchall()
        items = tuple(
            FrozenTaskItem(position=r["position"], code=r["item_code"], category=r["category"],
                           content=r["content"], sources=tuple(json.loads(r["sources_json"])))
            for r in item_rows
        )
        return InspectionTask(
            task_id=row["task_id"], site_id=row["site_id"], task_date=row["task_date"],
            status=row["status"], effective_at=context["effective_at"],
            process_profiles=tuple(context["process_profiles"]),
            enterprise_tags=tuple(context["enterprise_tags"]),
            version_ids=tuple(context["version_ids"]),
            override_ids=tuple(context["override_ids"]),
            removals=tuple(context.get("removals", [])),
            snapshot_hash=row["snapshot_hash"], created_by=row["created_by"],
            created_at=row["created_at"], items=items,
        )

    def list_tasks(self, site_id: str) -> list[InspectionTask]:
        self._site(self.database.connection, site_id)
        rows = self.database.connection.execute(
            "SELECT task_id FROM inspection_tasks WHERE site_id=? ORDER BY task_date", (site_id,)
        ).fetchall()
        return [self.get_task(row["task_id"]) for row in rows]

    def get_version(self, version_id: str) -> TemplateVersion:
        return self._load_version(self.database.connection,
                                  self._identifier(version_id, "version_id"))

    def list_versions(self, template_id: str) -> list[TemplateVersion]:
        template_id = self._identifier(template_id, "template_id")
        if self.database.connection.execute(
                "SELECT 1 FROM checklist_templates WHERE template_id=?",
                (template_id,)).fetchone() is None:
            raise NotFoundError("清单模板不存在")
        rows = self.database.connection.execute(
            "SELECT * FROM checklist_template_versions WHERE template_id=? ORDER BY version_no",
            (template_id,),
        ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def list_templates(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT t.template_id, t.name, t.version_count, t.created_by, t.created_at, "
            "COUNT(v.version_id) AS published_count FROM checklist_templates t "
            "LEFT JOIN checklist_template_versions v "
            "ON v.template_id=t.template_id AND v.status='published' "
            "GROUP BY t.template_id ORDER BY t.created_at, t.template_id"
        ).fetchall()
        return [{
            "template_id": row["template_id"],
            "name": row["name"],
            "version_count": row["version_count"],
            "published_count": row["published_count"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        } for row in rows]

    def list_overrides(self, site_id: str) -> list[EnterpriseOverride]:
        self._site(self.database.connection, site_id)
        return self._site_overrides(self.database.connection, site_id)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def _version_summary(self, version: TemplateVersion) -> dict[str, Any]:
        return {
            "version_id": version.version_id,
            "template_id": version.template_id,
            "version_no": version.version_no,
            "status": version.status,
            "applicability": {
                "process_profiles": sorted(version.applicability.process_profiles),
                "enterprise_tags": sorted(version.applicability.enterprise_tags),
            },
            "effective_from": version.effective_from,
            "effective_to": version.effective_to,
        }

    def _override_summary(self, override: EnterpriseOverride) -> dict[str, Any]:
        return {
            "override_id": override.override_id,
            "site_id": override.site_id,
            "template_id": override.template_id,
            "kind": override.kind,
            "item_code": override.item_code,
            "reason": override.reason,
            "valid_from": override.valid_from,
            "valid_to": override.valid_to,
            "status": override.status,
        }

    def _frozen_item_dict(self, item) -> dict[str, Any]:
        return {
            "position": item.position,
            "code": item.code,
            "category": item.category,
            "content": item.content,
            "sources": list(item.sources),
        }

    def task_dict(self, task: InspectionTask) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "site_id": task.site_id,
            "task_date": task.task_date,
            "status": task.status,
            "resolution": {
                "effective_at": task.effective_at,
                "process_profiles": list(task.process_profiles),
                "enterprise_tags": list(task.enterprise_tags),
                "version_ids": list(task.version_ids),
                "override_ids": list(task.override_ids),
                "removals": list(task.removals),
            },
            "snapshot_hash": task.snapshot_hash,
            "created_by": task.created_by,
            "created_at": task.created_at,
            "items": [self._frozen_item_dict(item) for item in task.items],
        }
