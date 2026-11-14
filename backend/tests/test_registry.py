"""服务注册发现核对测试：组合定位、健康轮廓、幂等注册、冲突裁决与路由降级。

核对口径来自业务要求：
- 服务名称、服务地址、注册版本可以组合定位，命中结果稳定排列；
- 注册时间、心跳间隔、健康状态可解释，陈旧心跳不能被展示为可用实例；
- 路由服务引用同一份健康状态，目标不可用时提前指向降级服务；
- 重复定位与并发注册不产生重复实例，编号与地址冲突时以实例编号为准；
- 刷新前后 REGI-0001 至 REGI-0003 的服务名称、服务地址、注册版本保持一致。
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.registry import registry_service
from app.store import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def restore_store():
    """每个用例结束后还原内存清单，用例之间互不影响。"""
    snapshot = copy.deepcopy(store._tables)
    yield
    store._tables.clear()
    store._tables.update(snapshot)


def list_registry(**params) -> dict:
    response = client.get("/api/registry", params=params)
    assert response.status_code == 200
    return response.json()


def find_by_code(code: str) -> dict:
    payload = list_registry(keyword=code)
    assert payload["total"] == 1
    return payload["items"][0]


# ---------- 组合定位与稳定排列 ----------

def test_service_name_and_address_filter_no_longer_return_all():
    by_name = list_registry(**{"服务名称": "payment-service"})
    assert by_name["total"] == 1
    assert by_name["items"][0]["实例编号"] == "REGI-0002"

    by_address = list_registry(**{"服务地址": "10.10.1.13"})
    assert by_address["total"] == 1
    assert by_address["items"][0]["实例编号"] == "REGI-0003"


def test_combined_locate_with_name_address_and_version():
    payload = list_registry(**{
        "服务名称": "payment-service",
        "服务地址": "10.10.1.12:8002",
        "服务版本": "v2.1.0",
    })
    assert payload["total"] == 1
    assert payload["items"][0]["实例编号"] == "REGI-0002"

    mismatched = list_registry(**{"服务名称": "payment-service", "服务版本": "v1.4.0"})
    assert mismatched["total"] == 0


def test_hits_are_stably_ordered_by_instance_code():
    first = list_registry(size=50)
    second = list_registry(size=50)
    codes = [item["实例编号"] for item in first["items"]]
    assert codes == sorted(codes)
    assert [item["实例编号"] for item in second["items"]] == codes


def test_keyword_search_and_pagination():
    page1 = list_registry(size=2, page=1)
    page2 = list_registry(size=2, page=2)
    assert page1["total"] == 4
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    codes = [item["实例编号"] for item in page1["items"] + page2["items"]]
    assert codes == ["REGI-0001", "REGI-0002", "REGI-0003", "REGI-0004"]

    hit = list_registry(keyword="REGI-0001")
    assert hit["total"] == 1
    assert hit["items"][0]["服务名称"] == "order-service"


# ---------- 健康轮廓 ----------

def test_health_profile_is_interpretable():
    healthy = find_by_code("REGI-0001")
    assert healthy["健康状态"] == "健康"
    assert healthy["可用"] is True
    assert "秒" in healthy["健康说明"]
    assert healthy["注册时间"] == "2026-09-01T09:00:00+08:00"
    assert healthy["心跳间隔"] == 15

    bad_interval = find_by_code("REGI-0002")
    assert bad_interval["健康状态"] == "心跳间隔异常"
    assert bad_interval["可用"] is False
    assert "0" in bad_interval["健康说明"]


def test_stale_heartbeat_is_never_shown_as_available():
    stale = find_by_code("REGI-0003")
    assert stale["健康状态"] == "心跳陈旧"
    assert stale["可用"] is False

    stats = client.get("/api/registry/stats").json()
    assert stats["total"] == 4
    assert stats["available"] == 2  # 只有 REGI-0001 与 REGI-0004 可用
    assert stats["unhealthy"] == 2


def test_heartbeat_action_refreshes_stale_instance():
    response = client.post("/api/registry/3/actions", json={"values": {"action": "上报心跳"}})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["entry"]["健康状态"] == "健康"
    assert payload["entry"]["可用"] is True


def test_abnormal_interval_instance_still_locatable_by_name_and_address():
    payload = list_registry(**{"服务名称": "payment-service", "服务地址": "10.10.1.12:8002"})
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["实例编号"] == "REGI-0002"
    assert item["健康状态"] == "心跳间隔异常"
    assert item["可用"] is False


# ---------- 幂等注册、版本不回退与冲突裁决 ----------

def register(values: dict) -> dict:
    response = client.post("/api/registry", json={"values": values})
    assert response.status_code == 200
    return response.json()


def test_version_never_rolls_back_on_refresh():
    older = register({
        "实例编号": "REGI-0002",
        "服务名称": "payment-service",
        "服务地址": "10.10.1.12:8002",
        "服务版本": "v2.0.0",
    })
    assert older["ok"] is True
    assert older["entry"]["服务版本"] == "v2.1.0"  # 旧版本不覆盖新版本

    newer = register({
        "实例编号": "REGI-0002",
        "服务名称": "payment-service",
        "服务地址": "10.10.1.12:8002",
        "服务版本": "v2.2.0",
    })
    assert newer["entry"]["服务版本"] == "v2.2.0"

    refreshed = find_by_code("REGI-0002")
    assert refreshed["服务版本"] == "v2.2.0"  # 刷新后清单中的注册版本不回退


def test_registry_identity_stable_across_refresh():
    def snapshot() -> list[tuple]:
        payload = list_registry(size=50)
        return [
            (item["实例编号"], item["服务名称"], item["服务地址"], item["服务版本"])
            for item in payload["items"]
            if item["实例编号"] in {"REGI-0001", "REGI-0002", "REGI-0003"}
        ]

    before = snapshot()
    for entry_id in (1, 2, 3):
        client.post(f"/api/registry/{entry_id}/actions", json={"values": {"action": "上报心跳"}})
    for code, name, address, _version in before:
        register({"实例编号": code, "服务名称": name, "服务地址": address, "服务版本": "v0.0.1"})
    assert snapshot() == before  # 刷新前后名称、地址、注册版本完全一致


def test_repeated_registration_creates_no_duplicate():
    values = {"实例编号": "REGI-9001", "服务名称": "risk-service", "服务地址": "10.10.9.1:9001"}
    first = register(values)
    second = register(values)
    assert first["ok"] is True and second["ok"] is True
    assert "幂等" in second["message"]
    assert list_registry(keyword="REGI-9001")["total"] == 1


def test_conflict_between_code_and_address_is_arbitrated_by_code():
    # 新编号抢占已占用地址 → 拒绝，并给出裁决依据
    taken_address = register({
        "实例编号": "REGI-9999",
        "服务名称": "squatter",
        "服务地址": "10.10.1.11:8001",
    })
    assert taken_address["ok"] is False
    assert "裁决依据" in taken_address["message"]
    assert "REGI-0001" in taken_address["message"]

    # 已有编号改绑别人的地址 → 同样拒绝
    hijack = register({
        "实例编号": "REGI-0001",
        "服务名称": "order-service",
        "服务地址": "10.10.1.12:8002",
    })
    assert hijack["ok"] is False
    assert "裁决依据" in hijack["message"]

    # 冲突被拒绝后清单不发生变化
    assert list_registry(size=50)["total"] == 4


def test_concurrent_registration_creates_single_instance():
    values = {"实例编号": "REGI-8001", "服务名称": "search-service", "服务地址": "10.10.8.1:8001"}

    def attempt(_: int):
        entry, _message = registry_service.create_entry(dict(values))
        return entry

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert all(entry is not None for entry in results)
    rows = [row for row in store.rows("registry") if row.get("实例编号") == "REGI-8001"]
    assert len(rows) == 1


# ---------- 路由服务引用同一份健康状态 ----------

def test_router_points_to_fallback_when_target_unavailable():
    response = client.get("/api/router", params={"size": 50})
    assert response.status_code == 200
    rules = {item["规则编号"]: item for item in response.json()["items"]}

    order = rules["ROUT-0001"]
    assert order["目标服务健康"] == "可用"
    assert order["路由指向"] == "order-service"

    payment = rules["ROUT-0002"]
    assert payment["目标服务健康"] == "不可用"
    assert payment["路由指向"] == "payment-mock"  # 心跳间隔异常 → 提前指向降级服务
    assert "降级服务" in payment["路由说明"]

    inventory = rules["ROUT-0003"]
    assert inventory["目标服务健康"] == "不可用"
    assert inventory["路由指向"] == "payment-mock"  # 陈旧心跳 → 同样提前降级


def test_router_follows_registry_health_after_heartbeat_refresh():
    before = client.get("/api/router", params={"keyword": "ROUT-0003"}).json()["items"][0]
    assert before["路由指向"] == "payment-mock"

    client.post("/api/registry/3/actions", json={"values": {"action": "上报心跳"}})

    after = client.get("/api/router", params={"keyword": "ROUT-0003"}).json()["items"][0]
    assert after["目标服务健康"] == "可用"
    assert after["路由指向"] == "inventory-service"  # 同一份健康状态，恢复后即回指目标服务
