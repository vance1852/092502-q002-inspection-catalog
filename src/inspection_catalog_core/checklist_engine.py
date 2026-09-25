"""清单适用性与覆盖层叠加的纯函数解析逻辑。

解析不触碰数据库与时钟，便于对叠加顺序、标签变化等场景做确定性测试。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .checklist_models import ChecklistItem, OverlayChange, ResolvedItem


def _as_str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{field} 必须是字符串数组")
    return sorted({v.strip() for v in value})


def item_from_dict(data: dict[str, Any]) -> ChecklistItem:
    """从 API/存储载荷构造条目并做基本校验。"""

    if not isinstance(data, dict):
        raise ValueError("条目必须是对象")
    code = str(data.get("code", "")).strip()
    content = str(data.get("content", "")).strip()
    category = str(data.get("category", "")).strip()
    if not code:
        raise ValueError("条目 code 不能为空")
    if not content:
        raise ValueError("条目 content 不能为空")
    if not category:
        raise ValueError("条目 category 不能为空")
    processes = frozenset(_as_str_list(data.get("applicable_processes"), "applicable_processes"))
    risks = frozenset(_as_str_list(data.get("risk_conditions"), "risk_conditions"))
    criteria = str(data.get("acceptance_criteria", "")).strip()
    return ChecklistItem(code, content, category, processes, risks, criteria)


def change_from_dict(data: dict[str, Any]) -> OverlayChange:
    """从载荷构造覆盖层变更。"""

    if not isinstance(data, dict):
        raise ValueError("变更必须是对象")
    kind = str(data.get("kind", "")).strip()
    if kind not in {"add", "remove", "modify"}:
        raise ValueError("变更 kind 只能是 add、remove、modify")
    code = str(data.get("code", "")).strip()
    if not code:
        raise ValueError("变更 code 不能为空")
    item: ChecklistItem | None = None
    if kind == "add":
        item = item_from_dict(data.get("item") or {"code": code, **{k: v for k, v in data.items() if k != "kind"}})
        if item.code != code:
            raise ValueError("add 变更的 code 必须与条目 code 一致")
    content = data.get("content")
    criteria = data.get("acceptance_criteria")
    if kind == "modify" and content is None and criteria is None:
        raise ValueError("modify 变更至少要提供 content 或 acceptance_criteria")
    return OverlayChange(kind, code, item,
                         str(content).strip() if content is not None else None,
                         str(criteria).strip() if criteria is not None else None)


def item_applies(item: ChecklistItem, processes: frozenset[str], risks: frozenset[str]) -> bool:
    """判断条目对某企业（工艺集合、风险/标签集合）是否适用。

    工艺、风险条件均为空表示无条件适用；否则相应集合需有交集。
    """

    if item.applicable_processes and not (item.applicable_processes & processes):
        return False
    if item.risk_conditions and not (item.risk_conditions & risks):
        return False
    return True


def _base_source(version_id: str) -> dict[str, Any]:
    return {"layer": "base", "version_id": version_id}


def resolve_checklist(
    *,
    version_id: str,
    items: Sequence[ChecklistItem],
    processes: Iterable[str],
    risks: Iterable[str],
    overlays: Sequence[tuple[str, int, Sequence[OverlayChange]]],
) -> list[ResolvedItem]:
    """按确定性顺序计算企业在某时点的最终条目。

    overlays 元素为 (overlay_id, revision, changes)，调用方必须已按
    (effective_from, created_at, overlay_id) 排好序。叠加规则：
    后生效的覆盖层覆盖先生效的覆盖层；同覆盖层内按 changes 顺序执行。
    """

    process_set = frozenset(processes)
    risk_set = frozenset(risks)

    # 以 code 为键维护工作集，保留基础条目顺序与新增条目顺序。
    working: dict[str, ResolvedItem] = {}
    order: list[str] = []
    for item in items:
        if not item_applies(item, process_set, risk_set):
            continue
        working[item.code] = ResolvedItem(
            item.code, item.content, item.category,
            tuple(sorted(item.applicable_processes)), tuple(sorted(item.risk_conditions)),
            item.acceptance_criteria, (_base_source(version_id),),
        )
        order.append(item.code)

    for overlay_id, revision, changes in overlays:
        for change in changes:
            source = {"layer": f"overlay_{change.kind}", "overlay_id": overlay_id, "revision": revision}
            if change.kind == "add":
                assert change.item is not None
                added = change.item
                if added.code in working:
                    # 新增条目与既有条目同 code：后生效覆盖层替换并记录来源。
                    sources = working[added.code].sources + (source,)
                else:
                    order.append(added.code)
                    sources = (source,)
                working[added.code] = ResolvedItem(
                    added.code, added.content, added.category,
                    tuple(sorted(added.applicable_processes)), tuple(sorted(added.risk_conditions)),
                    added.acceptance_criteria, sources,
                )
            elif change.kind == "remove":
                if change.code in working:
                    # 移除即从工作集删除；移除事实保留在审计与任务来源快照中。
                    del working[change.code]
                    order.remove(change.code)
            else:  # modify
                if change.code not in working:
                    # 修改目标不存在则跳过：基础条目不适用或已被移除，
                    # 覆盖层不能凭空改变适用性。
                    continue
                current = working[change.code]
                working[change.code] = ResolvedItem(
                    current.code,
                    change.content if change.content is not None else current.content,
                    current.category,
                    current.applicable_processes, current.risk_conditions,
                    change.acceptance_criteria if change.acceptance_criteria is not None
                    else current.acceptance_criteria,
                    current.sources + (source,),
                )

    return [working[code] for code in order]
