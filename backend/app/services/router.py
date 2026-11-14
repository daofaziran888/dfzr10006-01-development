"""查询路由分发业务规则：状态流转、字段校验与筛选口径都收在这里。

路由规则的目标服务可用性直接引用服务注册发现的同一份健康判定：
目标服务无可用实例时，列表会提前把路由指向降级服务，并给出可读的原因说明。
"""
from __future__ import annotations

from typing import Any

from app.services.registry import registry_service
from app.store import store

MODULE = "router"
REQUIRED_FIELDS = ["规则编号", "匹配条件", "目标服务"]
STATUS_ORDER = ["草稿", "生效中", "已降级", "已关闭"]
ACTION_RULES = {"启用规则": "草稿", "降级路由": "生效中", "关闭规则": "已降级"}
NEGATIVE_ACTIONS = []


def enrich_with_health(row: dict[str, Any]) -> dict[str, Any]:
    """给路由规则附上目标服务健康结论与当前实际路由指向。"""
    entry = dict(row)
    target = str(row.get("目标服务") or "").strip()
    fallback = str(row.get("降级服务") or "").strip()
    health = registry_service.service_availability(target)
    entry["目标服务健康"] = "可用" if health["可用"] else "不可用"
    if health["可用"]:
        entry["路由指向"] = target
        entry["路由说明"] = f"目标服务可用（{health['说明']}），按规则分发"
    elif fallback:
        entry["路由指向"] = fallback
        entry["路由说明"] = f"目标服务不可用（{health['说明']}），已提前指向降级服务 {fallback}"
    else:
        entry["路由指向"] = target
        entry["路由说明"] = f"目标服务不可用（{health['说明']}），且未配置降级服务"
    return entry


class RouterService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [enrich_with_health(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return enrich_with_health(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"路由规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询路由分发可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"路由规则已{action}"
