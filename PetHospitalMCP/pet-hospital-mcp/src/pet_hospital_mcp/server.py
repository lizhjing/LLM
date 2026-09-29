"""MCP 服务装配与启动入口。

关键设计：
- 使用 SDK 2.x 的 :class:`mcp.server.mcpserver.MCPServer`（不使用 FastMCP）；
- 传输为 **无状态 Streamable HTTP**（``stateless_http=True``），端点 ``/mcp``；
- 不实现 initialize 握手、Mcp-Session-Id、会话存储/过期、有状态 SSE 恢复；
  2026-07-28 客户端通过 ``server/discover`` 发现能力后直接 ``tools/call``；
- 保留独立的 ``GET /health`` 健康检查端点；
- 教学场景不实现认证、权限、CORS / Origin 校验。
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

from . import PROTOCOL_VERSION, SDK_VERSION, __version__
from .config import Settings, load_settings
from .logging_config import configure_logging, get_logger
from .rest_client import PetHospitalClient
from .tools.list_pets import register_list_pets_tool

logger = get_logger("server")

SERVER_NAME = "pet-hospital-mcp"
MCP_PATH = "/mcp"


def build_server(
    *,
    settings: Settings | None = None,
    client: PetHospitalClient | None = None,
) -> MCPServer:
    """创建并装配 MCPServer（工厂函数，便于测试注入 mock 客户端）。

    Args:
        settings: 运行配置，缺省从环境变量加载。
        client: 可选的上游 REST 客户端（测试时可注入带 MockTransport 的实例）。
    """
    settings = settings or load_settings()

    if client is None:
        client = PetHospitalClient(
            settings.base_url,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
            retry_backoff=settings.retry_backoff,
        )

    server = MCPServer(
        name=SERVER_NAME,
        title="Pet Hospital MCP Server",
        description="通过无状态 Streamable HTTP 暴露 Go 宠物医院 REST API 的 MCP 服务",
        instructions=(
            "本服务提供宠物医院档案查询工具 list_pets。"
            "业务数据全部来自上游 Go REST API，MCP 层不保存任何业务状态。"
        ),
        version=__version__,
    )

    # 唯一工具（本次不实现阶段二的其他工具）
    register_list_pets_tool(server, client)

    @server.custom_route("/health", methods=["GET"])
    async def health_check(request: object) -> JSONResponse:  # noqa: ARG001
        """轻量健康检查：只反映本进程存活与配置，不触发上游调用。"""
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVER_NAME,
                "version": __version__,
                "sdk_version": f"mcp {SDK_VERSION}",
                "protocol_version": PROTOCOL_VERSION,
                "transport": "stateless-streamable-http",
                "mcp_endpoint": MCP_PATH,
                "upstream_base_url": settings.base_url,
            }
        )

    return server


def build_app(
    *,
    settings: Settings | None = None,
    client: PetHospitalClient | None = None,
):
    """构建无状态 Streamable HTTP Starlette 应用（供 uvicorn / 进程内测试使用）。"""
    settings = settings or load_settings()
    server = build_server(settings=settings, client=client)
    return server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,  # 每个 POST 回单个 JSON，适配无状态工具调用场景
        stateless_http=True,  # 无状态：不分配/不要求 Mcp-Session-Id
        host=settings.host,
    )


def main() -> None:
    """命令行入口：加载配置并以无状态 Streamable HTTP 运行。"""
    configure_logging()
    try:
        settings = load_settings()
    except ValueError as exc:
        logger.error("配置无效：%s", exc)
        sys.stderr.write(f"配置无效：{exc}\n")
        raise SystemExit(2) from exc

    server = build_server(settings=settings)
    logger.info(
        "starting MCP server",
        extra={
            "event_fields": {
                "host": settings.host,
                "port": settings.port,
                "mcp_endpoint": MCP_PATH,
                "upstream_base_url": settings.base_url,
                "protocol_version": PROTOCOL_VERSION,
            }
        },
    )
    # 同步阻塞运行（SDK 内部使用 anyio + uvicorn）
    server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
