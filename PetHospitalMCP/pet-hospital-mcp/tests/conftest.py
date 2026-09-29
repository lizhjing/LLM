"""pytest 公共夹具。

所有测试都使用 httpx.MockTransport / ASGITransport 在进程内完成，
**禁止访问真实 Go 服务**。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalClient
from pet_hospital_mcp.server import build_server

BASE_URL = "http://pet-hospital.test"


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        base_url=BASE_URL,
        host="127.0.0.1",
        port=8000,
        request_timeout=5.0,
        max_retries=2,
        retry_backoff=0.0,  # 测试不等待
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest_asyncio.fixture
async def make_rest_client() -> Callable[..., PetHospitalClient]:
    """构造绑定 MockTransport 的上游客户端（测试结束自动关闭）。"""
    created: list[PetHospitalClient] = []

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        max_retries: int = 2,
        timeout: float = 5.0,
        backoff: float = 0.0,
    ) -> PetHospitalClient:
        client = PetHospitalClient(
            BASE_URL,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=backoff,
            transport=httpx.MockTransport(handler),
        )
        created.append(client)
        return client

    yield _factory

    for client in created:
        await client.aclose()


@pytest.fixture
def success_handler() -> Callable[[httpx.Request], httpx.Response]:
    """模拟 Go 成功响应：含 records/charges 为 null 的档案与非空档案各一条。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 200,
                "message": "ok",
                "data": {
                    "items": [
                        {
                            "id": "PET-000001",
                            "name": "旺财",
                            "species": "犬",
                            "ownerName": "张三",
                            "ownerPhone": "13800000000",
                            "doctor": "李医生",
                            "disease": "肠胃炎",
                            "status": "就诊中",
                            "records": None,
                            "charges": None,
                            "totalCost": 0,
                            "visitCount": 0,
                            "createdAt": "2026-09-01T10:00:00Z",
                            "updatedAt": "2026-09-01T10:00:00Z",
                        },
                        {
                            "id": "PET-000002",
                            "name": "咪咪",
                            "species": "猫",
                            "ownerName": "李四",
                            "ownerPhone": "13900000000",
                            "ownerAddr": "某市某区某路1号",
                            "chipNo": "CHIP-999",
                            "doctor": "王医生",
                            "disease": "猫瘟",
                            "status": "住院中",
                            "records": [
                                {
                                    "id": "R-1",
                                    "visitDate": "2026-09-02",
                                    "doctor": "王医生",
                                    "diagnosis": "猫瘟",
                                    "charge": 1200.5,
                                }
                            ],
                            "charges": [
                                {
                                    "id": "T-1",
                                    "item": "血常规检查",
                                    "category": "检查",
                                    "amount": 380,
                                    "date": "2026-09-02",
                                }
                            ],
                            "totalCost": 1200.5,
                            "visitCount": 1,
                            "createdAt": "2026-09-01T11:00:00Z",
                            "updatedAt": "2026-09-02T09:00:00Z",
                        },
                    ],
                    "total": 2,
                    "page": 1,
                    "pageSize": 20,
                    "totalPages": 1,
                    "totalCost": 1200.5,
                },
                "time": "2026-09-02T09:30:00Z",
            },
        )

    return handler


@pytest_asyncio.fixture
async def mcp_server(settings: Settings, make_rest_client, success_handler):
    client = make_rest_client(success_handler)
    return build_server(settings=settings, client=client)


@pytest_asyncio.fixture
async def mcp_stack(settings: Settings, make_rest_client, success_handler):
    """返回 (MCPServer, Starlette app)。

    进程内测试不经过 uvicorn，需由测试自行进入
    ``server.session_manager.run()``（等价于 Starlette lifespan 启动）。
    """
    from pet_hospital_mcp.server import MCP_PATH

    client = make_rest_client(success_handler)
    server = build_server(settings=settings, client=client)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        host=settings.host,
    )
    return server, app
