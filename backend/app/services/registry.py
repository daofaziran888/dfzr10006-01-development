"""服务注册发现业务规则：实例定位、健康轮廓判定与注册冲突裁决都收在这里。

【组合定位与稳定排列】
同一次定位可以组合 实例编号（keyword）、服务名称、服务地址、注册版本（服务版本）多个条件，
条件之间为与关系；命中结果按实例编号稳定排列，刷新、翻页不改变顺序。

【健康轮廓解释口径】
注册时间、心跳间隔、最近心跳时间共同决定健康状态：
- 已注册、心跳间隔为正数秒、最近心跳未超过 2 倍间隔 → 健康（可用实例）；
- 最近心跳超过 2 倍间隔 → 心跳陈旧，不是可用实例，不能被展示为可用；
- 心跳间隔为 0、负数或无法解释 → 心跳间隔异常，按不可用处理；
- 草稿、已下线、已注销 → 未在服，不参与可用性判定。

【冲突裁决依据：以实例编号为准】
实例编号是登记时分配的身份标识，全生命周期不变；服务地址只是部署属性，
可能随迁移、扩容被复用。因此：
- 实例编号命中 → 按幂等重注册处理，更新部署属性、刷新心跳，不产生重复实例；
- 实例编号未命中但服务地址已被占用 → 拒绝登记，防止同一地址指向两个身份；
- 编号命中、但传入地址被其他实例占用 → 同样拒绝，需先注销占用方。
注册版本只升不降：重注册携带的旧版本不会覆盖清单中已有的新版本。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "registry"
REQUIRED_FIELDS = ["实例编号", "服务名称", "服务地址"]
STATUS_ORDER = ["草稿", "已注册", "已下线", "已注销"]

# 动作 → (允许的来源状态, 目标状态)；上报心跳不改动状态，只刷新最近心跳时间。
TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "上线服务": ({"草稿"}, "已注册"),
    "下线服务": ({"已注册"}, "已下线"),
    "注销服务": ({"已下线"}, "已注销"),
    "上报心跳": ({"已注册"}, "已注册"),
}
NEGATIVE_ACTIONS = ["下线服务"]

HEALTH_OK = "健康"
HEALTH_STALE = "心跳陈旧"
HEALTH_BAD_INTERVAL = "心跳间隔异常"
HEALTH_OFFLINE = "未在服"
STALE_FACTOR = 2  # 最近心跳超过 心跳间隔 × STALE_FACTOR 即判定陈旧
DEFAULT_INTERVAL = 15  # 新实例缺省心跳间隔（秒）

# 并发注册共用一把锁：检查重复与写入清单必须原子，否则并发下会产生重复实例。
_CREATE_LOCK = threading.Lock()

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")
_INTERVAL_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:秒|s|sec|seconds)?\s*$", re.IGNORECASE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_interval(value: Any) -> float | None:
    """把心跳间隔解释成正数秒数；无法解释或不大于 0 时返回 None（间隔异常）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        match = _INTERVAL_RE.match(str(value or ""))
        if not match:
            return None
        seconds = float(match.group(1))
    return seconds if seconds > 0 else None


def parse_instant(value: Any) -> datetime | None:
    """把注册时间、最近心跳时间解释成带时区的时间点；无法解释时返回 None。"""
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant


def parse_version(text: Any) -> tuple[int, ...] | None:
    """把注册版本解释成可比较的元组，如 v2.1.0 → (2, 1, 0)；无法解释返回 None。"""
    if text is None:
        return None
    match = _VERSION_RE.search(str(text))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def merge_version(current: Any, incoming: str) -> Any:
    """注册版本只升不降：入参版本更旧或无法解释时保留清单中的版本。"""
    current_key = parse_version(current)
    incoming_key = parse_version(incoming)
    if incoming_key is None:
        return current
    if current_key is None:
        return incoming
    return incoming if incoming_key > current_key else current


def evaluate_health(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """解释单条实例的健康轮廓：健康状态、是否可用、以及一句可读的判定说明。"""
    now = now or utc_now()
    status = str(row.get("status") or "")
    if status != "已注册":
        return {
            "健康状态": HEALTH_OFFLINE,
            "可用": False,
            "健康说明": f"实例处于「{status or '未知'}」，未在服，不参与可用性判定",
        }
    interval = parse_interval(row.get("心跳间隔"))
    if interval is None:
        return {
            "健康状态": HEALTH_BAD_INTERVAL,
            "可用": False,
            "健康说明": f"心跳间隔「{row.get('心跳间隔')}」不是正数秒数，配置异常，按不可用处理",
        }
    last_beat = parse_instant(row.get("最近心跳时间"))
    if last_beat is None:
        return {
            "健康状态": HEALTH_STALE,
            "可用": False,
            "健康说明": "尚未收到心跳上报，按陈旧处理，不是可用实例",
        }
    elapsed = (now - last_beat).total_seconds()
    limit = interval * STALE_FACTOR
    if elapsed > limit:
        return {
            "健康状态": HEALTH_STALE,
            "可用": False,
            "健康说明": f"最近心跳距今 {elapsed:.0f} 秒，超过 {interval:g} 秒间隔的 {STALE_FACTOR} 倍宽限，判定陈旧",
        }
    return {
        "健康状态": HEALTH_OK,
        "可用": True,
        "健康说明": f"最近心跳距今 {elapsed:.0f} 秒，处于 {interval:g} 秒间隔的 {STALE_FACTOR} 倍宽限内",
    }


def enrich_entry(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """读取侧统一附上实时健康判定，清单、明细、导出看到的都是同一份结论。"""
    entry = dict(row)
    entry.update(evaluate_health(row, now))
    return entry


class RegistryService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        service_name: str | None = None,
        service_address: str | None = None,
        version: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        now: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """组合定位：实例编号检索 + 服务名称/服务地址/注册版本过滤，结果按实例编号稳定排列。"""
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("实例编号", ""))]
        if service_name:
            rows = [row for row in rows if service_name in str(row.get("服务名称", ""))]
        if service_address:
            rows = [row for row in rows if service_address in str(row.get("服务地址", ""))]
        if version:
            rows = [row for row in rows if version in str(row.get("服务版本", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = sorted(rows, key=lambda row: (str(row.get("实例编号", "")), int(row.get("id", 0))))
        total = len(rows)
        start = max(page - 1, 0) * size
        return [enrich_entry(row, now) for row in rows[start:start + size]], total

    def stats(self, now: datetime | None = None) -> dict[str, int]:
        """列表页统计卡片：总数、已注册、健康可用、心跳异常（含陈旧与间隔异常）。"""
        rows = [enrich_entry(row, now) for row in store.rows(MODULE)]
        registered = [row for row in rows if row.get("status") == "已注册"]
        return {
            "total": len(rows),
            "registered": len(registered),
            "available": sum(1 for row in registered if row.get("可用")),
            "unhealthy": sum(1 for row in registered if not row.get("可用")),
        }

    def get_entry(self, entry_id: int, now: datetime | None = None) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return enrich_entry(row, now) if row is not None else None

    def create_entry(
        self,
        values: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """登记或幂等重注册服务实例；冲突时按「以实例编号为准」裁决，版本只升不降。"""
        now = now or utc_now()
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        code = str(values["实例编号"]).strip()
        address = str(values["服务地址"]).strip()
        with _CREATE_LOCK:
            rows = store.rows(MODULE)
            by_code = next((row for row in rows if str(row.get("实例编号")) == code), None)
            by_addr = next((row for row in rows if str(row.get("服务地址")) == address), None)
            if by_code is not None:
                if by_addr is not None and by_addr is not by_code:
                    return None, (
                        f"服务地址 {address} 已被实例 {by_addr.get('实例编号')} 占用；"
                        f"裁决依据：以实例编号为准，{code} 不能抢占该地址，请先注销占用方"
                    )
                # 幂等重注册：同一编号重复定位/重复登记只更新属性，不产生重复实例。
                for field in ("服务名称", "服务地址", "心跳间隔", "实例标签"):
                    incoming = values.get(field)
                    if str(incoming or "").strip():
                        by_code[field] = incoming
                incoming_version = str(values.get("服务版本") or "").strip()
                if incoming_version:
                    by_code["服务版本"] = merge_version(by_code.get("服务版本"), incoming_version)
                by_code["最近心跳时间"] = now.isoformat()
                return enrich_entry(by_code, now), f"实例 {code} 已存在，按幂等重注册处理，注册版本不回退"
            if by_addr is not None:
                return None, (
                    f"服务地址 {address} 已被实例 {by_addr.get('实例编号')} 占用；"
                    f"裁决依据：以实例编号为准，新编号 {code} 不能登记同一地址"
                )
            entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry["实例编号"] = code
            entry["服务名称"] = str(values["服务名称"]).strip()
            entry["服务地址"] = address
            entry["服务版本"] = str(values.get("服务版本") or "").strip() or "v0.1.0"
            entry["注册时间"] = now.isoformat()
            entry["心跳间隔"] = values.get("心跳间隔") or DEFAULT_INTERVAL
            entry["最近心跳时间"] = now.isoformat()
            entry["健康状态"] = "待判定"
            entry["实例标签"] = str(values.get("实例标签") or "")
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return enrich_entry(entry, now), "服务实例已登记"

    def run_action(
        self,
        entry_id: int,
        action: str,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        now = now or utc_now()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务实例 {entry_id} 不存在或已归档"
        if action not in TRANSITIONS:
            return None, f"动作「{action}」不属于服务注册发现可执行范围"
        sources, target = TRANSITIONS[action]
        current = str(entry.get("status") or "")
        if current not in sources:
            return None, f"实例当前为「{current}」，不能执行「{action}」（允许来源：{'、'.join(sorted(sources))}）"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "上报心跳":
            entry["最近心跳时间"] = now.isoformat()
            return enrich_entry(entry, now), "心跳已上报，健康状态按最新心跳重新判定"
        return enrich_entry(entry, now), f"服务实例已{action}"

    def service_availability(self, service_name: Any, now: datetime | None = None) -> dict[str, Any]:
        """按服务名称汇总实例可用性；路由等其他业务面引用这同一份健康判定。"""
        name = str(service_name or "").strip()
        rows = [enrich_entry(row, now) for row in store.rows(MODULE) if str(row.get("服务名称")) == name]
        if not rows:
            return {"服务名称": name, "可用": False, "说明": f"服务 {name or '（空）'} 在注册清单中没有实例"}
        available = [row for row in rows if row.get("可用")]
        if available:
            return {"服务名称": name, "可用": True, "说明": f"{len(available)}/{len(rows)} 个实例可用"}
        reasons = "；".join(f"{row.get('实例编号')} {row.get('健康状态')}" for row in rows)
        return {"服务名称": name, "可用": False, "说明": f"无可用实例（{reasons}）"}


registry_service = RegistryService()
