"""REST 客户端测试：参数转发、超时/有限重试、错误分类、非法响应。

全部使用 httpx.MockTransport，不访问真实 Go 服务。
"""

from __future__ import annotations

import httpx
import pytest

from pet_hospital_mcp.errors import (
    BackendAPIError,
    BackendInvalidResponseError,
    BackendTimeoutError,
    BackendUnavailableError,
)
from pet_hospital_mcp.rest_client import PETS_PATH, build_pets_query


def test_build_pets_query_whitelist_and_none_skipping() -> None:
    query = build_pets_query(
        {
            "q": "肠胃炎",
            "name": None,
            "page": 2,
            "pageSize": 50,
            "min": 100.5,
            "species": "犬",
        }
    )
    assert query == {
        "q": "肠胃炎",
        "page": "2",
        "pageSize": "50",
        "min": "100.5",
        "species": "犬",
    }
    assert "name" not in query

    with pytest.raises(TypeError):
        build_pets_query({"unknown_param": 1})


async def test_success_forwards_all_params(make_rest_client) -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"code": 200, "data": {"items": [], "total": 0, "page": 3,
                                                                "pageSize": 15, "totalPages": 0,
                                                                "totalCost": 0}})

    client = make_rest_client(handler, max_retries=1)
    params = {
        "q": "关键词",
        "name": "旺财",
        "ownerName": "张三",
        "ownerPhone": "13800000000",
        "species": "犬",
        "doctor": "李医生",
        "disease": "肠胃炎",
        "status": "就诊中",
        "min": 100,
        "max": 5000.5,
        "sortBy": "totalCost",
        "order": "asc",
        "page": 3,
        "pageSize": 15,
    }
    payload = await client.list_pets(params)

    request = seen["request"]
    assert request.method == "GET"
    assert request.url.path == PETS_PATH
    # 全部 14 个参数逐一核对
    qp = request.url.params
    assert qp["q"] == "关键词"
    assert qp["name"] == "旺财"
    assert qp["ownerName"] == "张三"
    assert qp["ownerPhone"] == "13800000000"
    assert qp["species"] == "犬"
    assert qp["doctor"] == "李医生"
    assert qp["disease"] == "肠胃炎"
    assert qp["status"] == "就诊中"
    assert float(qp["min"]) == 100
    assert float(qp["max"]) == 5000.5
    assert qp["sortBy"] == "totalCost"
    assert qp["order"] == "asc"
    assert int(qp["page"]) == 3
    assert int(qp["pageSize"]) == 15
    assert len(qp) == 14
    assert payload["data"]["total"] == 0


async def test_client_error_4xx_is_backend_api_error(make_rest_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 400, "message": "pageSize 超过上限 500", "data": None})

    client = make_rest_client(handler, max_retries=2)
    with pytest.raises(BackendAPIError) as exc_info:
        await client.list_pets({"pageSize": 999})
    assert exc_info.value.details["http_status"] == 400
    assert exc_info.value.details["upstream_message"] == "pageSize 超过上限 500"
    assert "httpx" not in exc_info.value.message
    assert "Traceback" not in exc_info.value.message


async def test_4xx_is_not_retried(make_rest_client) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"code": 404, "message": "not found"})

    client = make_rest_client(handler, max_retries=3)
    with pytest.raises(BackendAPIError):
        await client.list_pets({})
    assert calls["n"] == 1


async def test_5xx_retried_then_succeeds(make_rest_client) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"code": 503, "message": "busy"})
        return httpx.Response(200, json={"code": 200, "data": {"items": [], "total": 0, "page": 1,
                                                                "pageSize": 20, "totalPages": 0,
                                                                "totalCost": 0}})

    client = make_rest_client(handler, max_retries=2)
    payload = await client.list_pets({})
    assert calls["n"] == 3
    assert payload["code"] == 200


async def test_5xx_exhausted_is_backend_api_error(make_rest_client) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="internal", headers={"content-type": "text/plain"})

    client = make_rest_client(handler, max_retries=1)
    with pytest.raises(BackendAPIError) as exc_info:
        await client.list_pets({})
    assert calls["n"] == 2  # 首次 + 1 次重试
    assert exc_info.value.details["http_status"] == 500


async def test_timeout_is_retried_then_backend_timeout(make_rest_client) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("simulated read timeout")

    client = make_rest_client(handler, max_retries=2)
    with pytest.raises(BackendTimeoutError) as exc_info:
        await client.list_pets({})
    assert calls["n"] == 3
    assert exc_info.value.details["attempts"] == 3
    assert "超时" in exc_info.value.message


async def test_connect_error_is_backend_unavailable(make_rest_client) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("[Errno 111] Connection refused")

    client = make_rest_client(handler, max_retries=2)
    with pytest.raises(BackendUnavailableError) as exc_info:
        await client.list_pets({})
    assert calls["n"] == 3
    assert exc_info.value.details["attempts"] == 3
    # 不泄露底层库异常细节
    assert "Errno" not in exc_info.value.message


async def test_no_retry_when_max_retries_zero(make_rest_client) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    client = make_rest_client(handler, max_retries=0)
    with pytest.raises(BackendUnavailableError):
        await client.list_pets({})
    assert calls["n"] == 1


async def test_invalid_json_is_invalid_response(make_rest_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="this is not json", headers={"content-type": "text/plain"})

    client = make_rest_client(handler)
    with pytest.raises(BackendInvalidResponseError) as exc_info:
        await client.list_pets({})
    assert exc_info.value.details["reason"] == "invalid_json"


async def test_non_object_json_is_invalid_response(make_rest_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = make_rest_client(handler)
    with pytest.raises(BackendInvalidResponseError) as exc_info:
        await client.list_pets({})
    assert exc_info.value.details["reason"] == "not_object"
