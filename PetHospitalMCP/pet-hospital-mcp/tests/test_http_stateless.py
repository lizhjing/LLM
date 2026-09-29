"""SDK 2.x 无状态 Streamable HTTP 端到端测试（进程内 ASGI，不访问真实网络）。

验证：
- 不发送旧版 initialize，使用 server/discover 完成 2026-07-28 现代握手；
- 服务端不要求、也不返回 Mcp-Session-Id；
- tools/list 可发现 list_pets，tools/call 可成功调用并返回结构化结果；
- 非法入参经 HTTP 仍返回统一错误结构；
- GET /health 可用。

注意：httpx 的 ASGITransport 不会触发 Starlette lifespan，因此测试需要显式
进入 ``server.session_manager.run()``（真实部署由 uvicorn 自动完成）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx2
import pytest_asyncio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from pet_hospital_mcp import PROTOCOL_VERSION
from pet_hospital_mcp.server import MCP_PATH, build_server

BASE_URL = "http://127.0.0.1:8000"


@pytest_asyncio.fixture
async def stack_with_500(settings, make_rest_client):
    import httpx

    client = make_rest_client(
        lambda request: httpx.Response(500, json={"code": 500, "message": "boom", "data": None}),
        max_retries=0,
    )
    server = build_server(settings=settings, client=client)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        host=settings.host,
    )
    return server, app


@asynccontextmanager
async def manager_lifespan(server):
    """模拟 uvicorn 对 Starlette lifespan 的启动/关闭。"""
    async with server.session_manager.run():
        yield


async def _recorded_http_client(app, request_log, response_log):
    async def on_request(request):  # noqa: ANN001
        request_log.append(request)

    async def on_response(response):  # noqa: ANN001
        response_log.append(response)

    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
        event_hooks={"request": [on_request], "response": [on_response]},
    )


async def test_stateless_discover_list_and_call(mcp_stack) -> None:
    server, app = mcp_stack
    request_log: list = []
    response_log: list = []

    async with manager_lifespan(server):
        http_client = await _recorded_http_client(app, request_log, response_log)
        async with http_client:
            async with streamable_http_client(
                f"{BASE_URL}{MCP_PATH}",
                http_client=http_client,
                terminate_on_close=False,  # 无状态：退出时不发 DELETE
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    # 现代流程：discover 而非 initialize
                    discover_result = await session.discover()

                    assert discover_result is not None
                    assert PROTOCOL_VERSION in discover_result.supported_versions
                    assert session.discover_result is not None
                    # 从未进行旧版 initialize
                    assert session.initialize_result is None
                    assert session.protocol_version == PROTOCOL_VERSION

                    # 能力中声明了 tools
                    assert discover_result.capabilities is not None

                    tools_result = await session.list_tools()
                    tool_names = [tool.name for tool in tools_result.tools]
                    assert tool_names == ["list_pets"]

                    # 成功调用
                    result = await session.call_tool(
                        "list_pets", {"species": "犬", "page": 1, "pageSize": 20}
                    )
                    assert result.is_error is False
                    data = result.structured_content
                    assert data["total"] == 2
                    assert set(data.keys()) == {
                        "items",
                        "total",
                        "page",
                        "pageSize",
                        "totalPages",
                        "totalCost",
                    }

                    # 非法输入：统一错误结构
                    bad = await session.call_tool("list_pets", {"species": "外星生物"})
                    assert bad.is_error is True
                    error = bad.structured_content["error"]
                    assert error["code"] == "VALIDATION_ERROR"
                    assert error["details"]["issues"]

                    # 未知字段必须被拒（证明工具读取的是 ctx 中的原始 arguments，
                    # 而不是 SDK 自动生成模型 extra=ignore 后过滤的 kwargs）
                    unknown = await session.call_tool("list_pets", {"totallyUnknown": 1})
                    assert unknown.is_error is True
                    assert (
                        unknown.structured_content["error"]["code"] == "VALIDATION_ERROR"
                    )

    # ---- 无状态断言（HTTP 层证据）----
    # 1) 任何响应都不携带 Mcp-Session-Id
    for response in response_log:
        assert not any(name.lower() == "mcp-session-id" for name in response.headers), (
            "无状态服务不得返回 Mcp-Session-Id"
        )
    # 2) 第一个请求就是现代 server/discover
    methods = [request.headers.get("mcp-method") for request in request_log]
    assert methods[0] == "server/discover"
    # 3) 全程没有 initialize
    assert "initialize" not in methods
    # 4) 每个请求都显式标注 2026-07-28
    for request in request_log:
        assert request.headers.get("mcp-protocol-version") == PROTOCOL_VERSION


async def test_tools_call_backend_error_over_http(stack_with_500) -> None:
    server, app = stack_with_500
    request_log: list = []
    response_log: list = []

    async with manager_lifespan(server):
        http_client = await _recorded_http_client(app, request_log, response_log)
        async with http_client:
            async with streamable_http_client(
                f"{BASE_URL}{MCP_PATH}", http_client=http_client, terminate_on_close=False
            ) as (rs, ws):
                async with ClientSession(rs, ws) as session:
                    await session.discover()
                    result = await session.call_tool("list_pets", {})

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "BACKEND_API_ERROR"
    assert result.structured_content["error"]["details"]["http_status"] == 500
    for response in response_log:
        assert not any(name.lower() == "mcp-session-id" for name in response.headers)


async def test_health_endpoint(mcp_stack) -> None:
    _server, app = mcp_stack
    # /health 是普通 Starlette 路由，不依赖 session manager lifespan
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as http_client:
        response = await http_client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["protocol_version"] == PROTOCOL_VERSION
    assert body["sdk_version"] == "mcp 2.0.0"
    assert body["transport"] == "stateless-streamable-http"
    assert body["mcp_endpoint"] == "/mcp"
    assert body["upstream_base_url"].startswith("http://")
