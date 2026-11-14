"""服务注册发现接口：维护服务实例，覆盖上线服务、下线服务、注销服务、上报心跳等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.registry import registry_service as service

router = APIRouter(prefix="/api/registry", tags=["服务注册发现"])

LIST_FIELDS = ["实例编号", "服务名称", "服务地址", "服务版本", "注册时间", "心跳间隔", "健康状态", "实例标签"]
STATUSES = ["草稿", "已注册", "已下线", "已注销"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按实例编号检索"),
    service_name: str | None = Query(default=None, alias="服务名称", description="按服务名称过滤"),
    service_address: str | None = Query(default=None, alias="服务地址", description="按服务地址过滤"),
    version: str | None = Query(default=None, alias="服务版本", description="按注册版本过滤"),
    status: str | None = Query(default=None, description="草稿、已注册、已下线、已注销"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """组合定位服务实例：实例编号、服务名称、服务地址、注册版本可叠加，结果按实例编号稳定排列。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        service_name=service_name,
        service_address=service_address,
        version=version,
        status=status,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/stats")
def stats() -> dict[str, int]:
    """列表页统计卡片：实例总数、已注册数、健康可用数、心跳异常数。"""
    return service.stats()


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条服务实例明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"服务实例 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记服务实例：重复编号按幂等重注册处理，地址冲突按裁决依据拒绝并说明原因。"""
    entry, message = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条服务实例执行上线服务、下线服务、注销服务、上报心跳；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出服务注册发现清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "registry", "total": total, "items": items}
