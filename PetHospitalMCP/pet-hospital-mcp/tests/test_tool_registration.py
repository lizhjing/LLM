"""工具注册与对外 JSON Schema 测试。"""

from __future__ import annotations

import re

from pet_hospital_mcp.tools.list_pets import (
    LIST_PETS_PARAM_NAMES,
    ORDER_VALUES,
    SORT_BY_VALUES,
    SPECIES_VALUES,
    STATUS_VALUES,
)


def _non_null(schema: dict) -> dict:
    """从 ``anyOf: [X, {type: null}]`` 中取出非 null 分支；普通 schema 原样返回。"""
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            if branch.get("type") != "null":
                return branch
    return schema


async def test_exactly_one_tool_registered(mcp_server) -> None:
    tools = await mcp_server.list_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "list_pets"
    assert re.fullmatch(r"[a-z][a-z0-9_]*", tool.name)  # snake_case
    assert tool.description
    for keyword in ("用途", "参数", "适用场景", "返回值"):
        assert keyword in tool.description


async def test_input_schema_properties_and_required(mcp_server) -> None:
    tool = (await mcp_server.list_tools())[0]
    schema = tool.input_schema
    assert schema["type"] == "object"
    # 14 个参数，一个不多一个不少
    assert set(schema["properties"].keys()) == set(LIST_PETS_PARAM_NAMES)
    # 拒绝未知字段
    assert schema["additionalProperties"] is False
    # 全部可选
    assert schema.get("required", []) == []


async def test_input_schema_enums(mcp_server) -> None:
    props = (await mcp_server.list_tools())[0].input_schema["properties"]
    assert _non_null(props["species"])["type"] == "string"
    assert _non_null(props["species"])["enum"] == list(SPECIES_VALUES)
    assert _non_null(props["status"])["enum"] == list(STATUS_VALUES)
    assert _non_null(props["sortBy"])["enum"] == list(SORT_BY_VALUES)
    assert _non_null(props["order"])["enum"] == list(ORDER_VALUES)


async def test_input_schema_numeric_constraints(mcp_server) -> None:
    props = (await mcp_server.list_tools())[0].input_schema["properties"]
    page = _non_null(props["page"])
    assert page["type"] == "integer"
    assert page["minimum"] == 1

    page_size = _non_null(props["pageSize"])
    assert page_size["type"] == "integer"
    assert page_size["minimum"] == 1
    assert page_size["maximum"] == 500

    min_cost = _non_null(props["min"])
    assert min_cost["type"] == "number"
    assert min_cost["minimum"] == 0

    max_cost = _non_null(props["max"])
    assert max_cost["type"] == "number"
    assert max_cost["minimum"] == 0


async def test_registered_tool_is_invokable_via_server_call_tool(
    mcp_server, make_rest_client, success_handler
) -> None:
    """通过 MCPServer.call_tool 走完整 SDK 调用链，验证统一结果结构。"""
    result = await mcp_server.call_tool(
        "list_pets",
        {"species": "犬", "page": 1},
        None,  # 不经协议时 context 允许为 None（工具内部做了降级）
    )
    assert result.is_error is False
    assert result.structured_content["total"] == 2
