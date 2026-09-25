"""巡查清单服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# 版本生命周期：草稿 -> 待复核 -> 已发布 -> （可回滚）
VERSION_DRAFT = "draft"
VERSION_PENDING_REVIEW = "pending_review"
VERSION_PUBLISHED = "published"
VERSION_ROLLED_BACK = "rolled_back"

# 企业覆盖层状态
OVERLAY_ACTIVE = "active"
OVERLAY_REVOKED = "revoked"

# 巡查任务状态
TASK_PENDING = "pending"
TASK_STARTED = "started"
TASK_COMPLETED = "completed"
TASK_CANCELLED = "cancelled"


@dataclass(frozen=True)
class ChecklistItem:
    """模板版本中的一个检查条目。

    applicable_processes 命中企业工艺（process_profile 的 external_key）时适用；
    risk_conditions 命中企业污染要素（pollution_factor）或标签（enterprise_tag）。
    两者均为空表示该条目对所有企业适用。
    """

    code: str
    content: str
    category: str
    applicable_processes: frozenset[str] = frozenset()
    risk_conditions: frozenset[str] = frozenset()
    acceptance_criteria: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "content": self.content,
            "category": self.category,
            "applicable_processes": sorted(self.applicable_processes),
            "risk_conditions": sorted(self.risk_conditions),
            "acceptance_criteria": self.acceptance_criteria,
        }


@dataclass(frozen=True)
class OverlayChange:
    """企业级覆盖层中的单条增删改。

    kind=add 时必须提供 item；kind=remove 时引用基础条目 code；
    kind=modify 时引用基础条目 code 并提供覆盖内容。
    """

    kind: str
    code: str
    item: ChecklistItem | None = None
    content: str | None = None
    acceptance_criteria: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "code": self.code,
            "item": self.item.to_dict() if self.item else None,
            "content": self.content,
            "acceptance_criteria": self.acceptance_criteria,
        }


@dataclass(frozen=True)
class ChecklistVersion:
    """一个模板版本及其完整状态。"""

    version_id: str
    template_id: str
    version_number: int
    status: str
    name: str
    items: tuple[ChecklistItem, ...]
    items_hash: str
    effective_from: str | None
    effective_until: str | None
    created_by: str
    created_at: str
    submitted_by: str | None
    submitted_at: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    review_decision: str | None
    review_comment: str | None
    published_at: str | None
    rolled_back_by: str | None
    rolled_back_at: str | None
    rollback_reason: str | None


@dataclass(frozen=True)
class ChecklistOverlay:
    """企业级覆盖层：有理由、有期限的条目增删改。"""

    overlay_id: str
    template_id: str
    site_id: str
    revision: int
    reason: str
    effective_from: str
    expires_at: str
    status: str
    changes: tuple[OverlayChange, ...]
    created_by: str
    created_at: str
    revoked_by: str | None
    revoked_at: str | None
    revoke_reason: str | None


@dataclass(frozen=True)
class ResolvedItem:
    """清单解析后的最终条目及其来源链。"""

    code: str
    content: str
    category: str
    applicable_processes: tuple[str, ...]
    risk_conditions: tuple[str, ...]
    acceptance_criteria: str
    sources: tuple[dict[str, Any], ...] = field(default=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "content": self.content,
            "category": self.category,
            "applicable_processes": list(self.applicable_processes),
            "risk_conditions": list(self.risk_conditions),
            "acceptance_criteria": self.acceptance_criteria,
            "sources": list(self.sources),
        }
