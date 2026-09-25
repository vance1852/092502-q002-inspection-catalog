"""实现适用版本匹配、企业覆盖合并与快照解析的纯领域逻辑。

时间统一使用 ISO-8601 字符串（UTC，以 ``Z`` 结尾），字典序与时间序一致，
因此生效区间比较可以直接使用字符串比较。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .models import Applicability, ChecklistItem, EnterpriseOverride, FrozenTaskItem, TemplateVersion


def half_open_overlaps(start_a: str, end_a: str | None,
                       start_b: str, end_b: str | None) -> bool:
    """判断两个半开区间 ``[start, end)`` 是否重叠。

    端点相接（``end_a == start_b``）不算重叠；``None`` 表示开放右端。
    """

    if end_a is not None and end_a <= start_b:
        return False
    if end_b is not None and end_b <= start_a:
        return False
    return True


def applicability_matches(applicability: Applicability,
                          process_profiles: Iterable[str],
                          enterprise_tags: Iterable[str]) -> bool:
    """判断站点的工艺/标签集合是否落在版本适用范围内。

    工艺维度必须有交集（版本工艺集合不允许为空）；
    风险标签维度在版本未声明标签时视为不限制，否则同样要求交集。
    """

    profiles = frozenset(process_profiles)
    if not profiles & applicability.process_profiles:
        return False
    if applicability.enterprise_tags:
        if not frozenset(enterprise_tags) & applicability.enterprise_tags:
            return False
    return True


def resolve_published_versions(versions: Sequence[TemplateVersion],
                               process_profiles: Iterable[str],
                               enterprise_tags: Iterable[str],
                               effective_at: str) -> list[TemplateVersion]:
    """返回某时刻对站点生效的全部模板版本。

    同一模板至多一个版本生效（发布阶段已拒绝重叠区间）；
    多个模板同时适用时，其条目按模板编号、版本号稳定排序后合并。
    """

    matched = [
        version for version in versions
        if version.status == "published"
        and version.effective_from is not None
        and version.effective_from <= effective_at
        and (version.effective_to is None or effective_at < version.effective_to)
        and applicability_matches(version.applicability, process_profiles, enterprise_tags)
    ]
    matched.sort(key=lambda version: (version.template_id, version.version_no))
    return matched


def _active_overrides(overrides: Sequence[EnterpriseOverride],
                      effective_at: str) -> list[EnterpriseOverride]:
    active = [
        override for override in overrides
        if override.status == "active"
        and override.valid_from <= effective_at < override.valid_to
    ]
    # 新建在后，因此后登记的覆盖优先；同刻以编号稳定排序。
    active.sort(key=lambda override: (override.created_at, override.override_id))
    return active


def resolve_checklist(versions: Sequence[TemplateVersion],
                      overrides: Sequence[EnterpriseOverride],
                      process_profiles: Iterable[str],
                      enterprise_tags: Iterable[str],
                      effective_at: str) -> dict[str, Any]:
    """把生效版本与企业覆盖合并成最终条目清单及逐项来源。

    合并规则（全部确定化）：

    1. 适用版本按模板、版本号排序后依次提供基线条目；
       不同模板出现相同条目编码时，排序靠后的模板胜出并同时记录来源。
    2. ``remove`` 覆盖删除对应编码的基线条目；
    3. ``add`` 覆盖在基线之后追加，相同编码以最后登记的覆盖为准；
    4. 不限定模板的覆盖对全部基线生效，限定模板的覆盖只影响该模板。

    返回解析上下文（版本、覆盖）与有序的冻结条目。
    """

    matched_versions = resolve_published_versions(versions, process_profiles,
                                                  enterprise_tags, effective_at)
    active_overrides = _active_overrides(overrides, effective_at)

    # 编码 -> (条目, 来源列表)
    resolved: dict[str, tuple[ChecklistItem, list[dict[str, str]]]] = {}
    for version in matched_versions:
        for item in version.items:
            sources = list(resolved.get(item.code, (None, []))[1])
            sources.append({
                "kind": "template_version",
                "template_id": version.template_id,
                "version_no": str(version.version_no),
                "version_id": version.version_id,
            })
            resolved[item.code] = (item, sources)

    # 按模板分组记录覆盖来源，未限定模板的覆盖作用于全部匹配版本。
    version_index = {version.template_id: version for version in matched_versions}
    removals: list[dict[str, Any]] = []
    for override in active_overrides:
        if override.template_id is not None and override.template_id not in version_index:
            continue
        if override.kind == "remove":
            if override.item_code in resolved:
                del resolved[override.item_code]
            # 即使基线已不含该编码，也保留覆盖痕迹，便于解释“为何缺项”。
            removals.append({
                "item_code": override.item_code,
                "override_id": override.override_id,
                "template_id": override.template_id,
            })
        else:
            assert override.content is not None
            source = {
                "kind": "enterprise_override",
                "override_id": override.override_id,
                "override_kind": "add",
                "site_id": override.site_id,
            }
            if override.template_id is not None:
                source["template_id"] = override.template_id
            previous_sources = list(resolved.get(override.item_code, (None, []))[1])
            resolved[override.item_code] = (
                override.content,
                previous_sources + [source],
            )

    ordered_codes = sorted(resolved)
    frozen_items = [
        FrozenTaskItem(
            position=position,
            code=code,
            category=resolved[code][0].category,
            content=resolved[code][0].content,
            sources=tuple(resolved[code][1]),
        )
        for position, code in enumerate(ordered_codes, start=1)
    ]
    return {
        "effective_at": effective_at,
        "process_profiles": tuple(sorted(process_profiles)),
        "enterprise_tags": tuple(sorted(enterprise_tags)),
        "matched_versions": matched_versions,
        "active_overrides": active_overrides,
        "removals": tuple(removals),
        "items": tuple(frozen_items),
    }
