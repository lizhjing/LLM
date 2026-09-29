"""list_pets 工具核心测试：成功映射、严格输入校验、统一错误结构。"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from pet_hospital_mcp.errors import ErrorCode
from pet_hospital_mcp.tools.list_pets import (
    LIST_PETS_PARAM_NAMES,
    ErrorEnvelope,
    execute_list_pets,
)


def _assert_unified_error(result, expected_code: str) -> dict:
    """断言 CallToolResult 携带统一错误信封并返回该信封。"""
    assert result.is_error is True
    body = result.structured_content
    assert isinstance(body, dict)
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert error["code"] == expected_code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    # text 内容与 structured_content 一致且是合法 JSON
    assert json.loads(result.content[0].text) == body
    # 错误信封自身满足错误输出模型
    ErrorEnvelope.model_validate(body)
    return body


async def test_success_returns_data_with_six_fields(make_rest_client, success_handler) -> None:
    client = make_rest_client(success_handler)
    result = await execute_list_pets(client, {"species": "犬", "page": 1, "pageSize": 20})

    assert result.is_error is False
    data = result.structured_content
    assert set(data.keys()) == {"items", "total", "page", "pageSize", "totalPages", "totalCost"}
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["pageSize"] == 20
    assert data["totalPages"] == 1
    assert data["totalCost"] == 1200.5
    # 兼容 Go records/charges 为 null
    assert data["items"][0]["records"] is None
    assert data["items"][0]["charges"] is None
    # 非空数组形态
    assert isinstance(data["items"][1]["records"], list)
    assert isinstance(data["items"][1]["charges"], list)
    # text 内容为同一 JSON
    assert json.loads(result.content[0].text) == data


async def test_success_forwards_only_provided_params(make_rest_client) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = set(request.url.params.keys())
        seen["species"] = request.url.params["species"]
        return httpx.Response(
            200,
            json={"code": 200, "data": {"items": [], "total": 0, "page": 1,
                                         "pageSize": 20, "totalPages": 0, "totalCost": 0}},
        )

    client = make_rest_client(handler)
    await execute_list_pets(client, {"species": "猫"})
    assert seen["params"] == {"species"}
    assert seen["species"] == "猫"


async def test_empty_arguments_is_valid(make_rest_client, success_handler) -> None:
    client = make_rest_client(success_handler)
    result = await execute_list_pets(client, {})
    assert result.is_error is False


@pytest.mark.parametrize(
    "arguments",
    [
        {"species": "狗"},                # 非法枚举
        {"status": "出院"},               # 非法状态
        {"sortBy": "password"},           # 非法排序字段
        {"order": "ASC"},                 # 大小写不允许
        {"order": "descending"},
        {"page": 0},                      # page >= 1
        {"page": -1},
        {"pageSize": 0},                  # 1 <= pageSize <= 500
        {"pageSize": 501},
        {"min": -0.01},                   # min 非负
        {"max": -1},
        {"min": 500, "max": 100},         # min <= max
        {"unknown_field": 1},             # 拒绝未知字段
        {"page": "1"},                    # 拒绝字符串数字（类型不正确）
        {"page": True},                   # 拒绝布尔
        {"page": 1.5},                    # 拒绝非整数浮点
        {"q": 123},                       # 字符串字段必须是字符串
        {"species": 7},                   # 枚举字段必须是字符串
        {"min": float("nan")},            # 拒绝 NaN
        {"max": float("inf")},            # 拒绝 Infinity
        {"min": float("-inf")},
        {"pageSize": "20"},               # 类型不正确
        {"species": ["犬"]},              # 不接受数组
    ],
)
async def test_invalid_inputs_return_validation_error(make_rest_client, success_handler, arguments) -> None:
    client = make_rest_client(success_handler)  # 校验失败时不应触达上游
    result = await execute_list_pets(client, arguments)
    body = _assert_unified_error(result, ErrorCode.VALIDATION_ERROR)
    issues = body["error"]["details"]["issues"]
    assert isinstance(issues, list) and issues
    assert {"field", "type", "message"} <= set(issues[0].keys())


async def test_non_object_arguments_return_validation_error(make_rest_client, success_handler) -> None:
    client = make_rest_client(success_handler)
    result = await execute_list_pets(client, ["not", "an", "object"])
    body = _assert_unified_error(result, ErrorCode.VALIDATION_ERROR)
    assert body["error"]["details"]["received_type"] == "list"


async def test_backend_4xx_maps_to_backend_api_error(make_rest_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"code": 502, "message": "bad gateway"})

    client = make_rest_client(handler, max_retries=0)
    result = await execute_list_pets(client, {})
    _assert_unified_error(result, ErrorCode.BACKEND_API_ERROR)


async def test_backend_timeout_maps_to_timeout_error(make_rest_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    client = make_rest_client(handler, max_retries=0)
    result = await execute_list_pets(client, {})
    _assert_unified_error(result, ErrorCode.BACKEND_TIMEOUT)


async def test_backend_unavailable_maps_to_unavailable_error(make_rest_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = make_rest_client(handler, max_retries=0)
    result = await execute_list_pets(client, {})
    _assert_unified_error(result, ErrorCode.BACKEND_UNAVAILABLE)


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 200, "data": None},                      # data 为 null
        {"code": 200, "data": {}},                        # data 缺字段
        {"code": 200, "data": {"items": [], "total": 0}},  # 字段不全
        {
            "code": 200,
            "data": {  # 类型错误
                "items": "not-a-list",
                "total": 0,
                "page": 1,
                "pageSize": 20,
                "totalPages": 0,
                "totalCost": 0,
            },
        },
        {
            "code": 200,
            "data": {  # items 元素 records 类型错误（不是 null/数组）
                "items": [{"records": "oops"}],
                "total": 1,
                "page": 1,
                "pageSize": 20,
                "totalPages": 1,
                "totalCost": 0,
            },
        },
    ],
)
async def test_invalid_data_shape_maps_to_invalid_response(make_rest_client, payload) -> None:
    client = make_rest_client(lambda request: httpx.Response(200, json=payload), max_retries=0)
    result = await execute_list_pets(client, {})
    _assert_unified_error(result, ErrorCode.BACKEND_INVALID_RESPONSE)


async def test_tool_does_not_raise_on_internal_failure(monkeypatch, make_rest_client, success_handler) -> None:
    client = make_rest_client(success_handler)

    def boom(_params):
        raise RuntimeError("unexpected internal detail XYZ")

    monkeypatch.setattr(client, "list_pets", boom)
    result = await execute_list_pets(client, {})
    body = _assert_unified_error(result, ErrorCode.INTERNAL_ERROR)
    # 内部细节绝不外泄
    assert "XYZ" not in json.dumps(body, ensure_ascii=False)


async def test_nan_infinity_serialize_safely_in_error(make_rest_client, success_handler) -> None:
    client = make_rest_client(success_handler)
    result = await execute_list_pets(client, {"min": math.nan})
    # 错误结构必须可被标准 JSON 序列化（不含裸 NaN token）
    serialized = json.dumps(result.structured_content, ensure_ascii=False)
    assert "NaN" not in serialized
    assert result.is_error is True


def test_param_name_whitelist_matches_go_api() -> None:
    assert LIST_PETS_PARAM_NAMES == (
        "q",
        "name",
        "ownerName",
        "ownerPhone",
        "species",
        "doctor",
        "disease",
        "status",
        "min",
        "max",
        "sortBy",
        "order",
        "page",
        "pageSize",
    )
