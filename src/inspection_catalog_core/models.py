"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示企业或监管组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class ChecklistItem:
    """表示清单中的一个巡查条目。"""

    code: str
    category: str
    content: str


@dataclass(frozen=True)
class Applicability:
    """描述版本适用的工艺集合与风险标签集合。

    工艺集合为必填且非空；风险标签集合为空表示不限风险条件。
    站点在两个维度上各自取交集，维度之间为“与”。
    """

    process_profiles: frozenset[str]
    enterprise_tags: frozenset[str]


@dataclass(frozen=True)
class TemplateVersion:
    """描述一个模板版本的完整状态。"""

    version_id: str
    template_id: str
    version_no: int
    status: str
    applicability: Applicability
    items: tuple[ChecklistItem, ...]
    content_hash: str
    drafted_by: str
    drafted_at: str
    submitted_by: str | None
    submitted_at: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    review_result: str | None
    review_reason: str | None
    published_by: str | None
    published_at: str | None
    effective_from: str | None
    effective_to: str | None
    revoked_by: str | None
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class EnterpriseOverride:
    """描述企业级有理由、有期限的增删项覆盖。"""

    override_id: str
    site_id: str
    template_id: str | None
    kind: str
    item_code: str
    content: ChecklistItem | None
    reason: str
    valid_from: str
    valid_to: str
    status: str
    created_by: str
    created_at: str
    revoked_by: str | None
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class FrozenTaskItem:
    """表示任务生成时冻结的条目及其全部来源。"""

    position: int
    code: str
    category: str
    content: str
    sources: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class InspectionTask:
    """表示一次每日巡查任务及其不可变清单快照。"""

    task_id: str
    site_id: str
    task_date: str
    status: str
    effective_at: str
    process_profiles: tuple[str, ...]
    enterprise_tags: tuple[str, ...]
    version_ids: tuple[str, ...]
    override_ids: tuple[str, ...]
    removals: tuple[dict[str, Any], ...]
    snapshot_hash: str
    created_by: str
    created_at: str
    items: tuple[FrozenTaskItem, ...]
