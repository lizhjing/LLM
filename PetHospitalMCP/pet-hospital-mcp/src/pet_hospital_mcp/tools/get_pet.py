"""第二个只读工具：``get_pet``，按 pet_id 查单条档案。

适配 Go 后端 ``GET /api/v1/pets/{id}``。错误信封与 ``list_pets`` 完全一致；
复用 :mod:`pet_hospital_mcp.rest_client` 的超时/重试/错误转换。
"""

from __future__ import annotations

import json
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict

from ..errors import (
    ErrorCode,
    InternalError,
    PetHospitalMCPError,
    error_body,
)
from ..logging_config import get_logger, log_tool_event
from ..rest_client import PetHospitalClient
from .list_pets import Pet

logger = get_logger("tools.get_pet")


class GetPetInput(BaseModel):
    """``get_pet`` 的严格输入模型。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    pet_id: str


class GetPetBackendResponse(BaseModel):
    """Go 顶层响应信封（仅关心 data）。"""

    model_config = ConfigDict(extra="allow")
    data: Pet


def _error_result(body: dict[str, Any]) -> CallToolResult:
    text = json.dumps(body, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=body,
        is_error=True,
    )


def _success_result(pet: Pet) -> CallToolResult:
    payload = pet.model_dump(mode="json", by_alias=True)
    text = json.dumps(payload, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=False,
    )


async def execute_get_pet(
    client: PetHospitalClient,
    pet_id: Any,
) -> CallToolResult:
    """按 pet_id 查询单条宠物档案，成功返回 Pet JSON，失败返回统一错误信封。"""
    started_at = time.perf_counter()

    if not isinstance(pet_id, str) or not pet_id.strip():
        body = error_body(
            ErrorCode.VALIDATION_ERROR,
            "get_pet 参数 pet_id 必须是非空字符串。",
            details={"received_type": type(pet_id).__name__},
        )
        log_tool_event(
            logger, tool_name="get_pet", params=pet_id,
            status="validation_error", started_at=started_at,
        )
        return _error_result(body)

    pet_id = pet_id.strip()
    try:
        envelope = await client.get_pet(pet_id)
        parsed = GetPetBackendResponse.model_validate(envelope)
    except PetHospitalMCPError as exc:
        body = exc.to_body()
        log_tool_event(
            logger, tool_name="get_pet", params=pet_id,
            status=exc.code, started_at=started_at, error_code=exc.code,
        )
        return _error_result(body)
    except Exception:  # noqa: BLE001
        logger.exception("unexpected error in get_pet")
        body = InternalError("服务内部错误。").to_body()
        log_tool_event(
            logger, tool_name="get_pet", params=pet_id,
            status="internal_error", started_at=started_at,
        )
        return _error_result(body)

    log_tool_event(
        logger, tool_name="get_pet", params=pet_id,
        status="success", started_at=started_at,
    )
    return _success_result(parsed.data)


GET_PET_DESCRIPTION = """\
按 pet_id 查询单条宠物档案（含完整病历 records 与收费 charges）。

用途：
  已知宠物档案 ID（如 PET-000001）时，直接拉取该条完整档案，比 list_pets 更精确。

参数：
  pet_id   宠物档案 ID（字符串），与 Go 后端主键一致。

适用场景：
  - “查一下 PET-000001 的完整档案”
  - 根据前一步 list_pets 返回的 id 继续深挖某一条记录

不支持新增/修改病历等写操作。

返回值（成功，structuredContent 即 Go Pet 对象）：
  { id, name, species, ownerName, records, charges, totalCost, visitCount, ... }
  records/charges 可能为 null 或数组。

失败时返回统一错误信封 {error: {code, message, details}}，isError=true；
错误码与 list_pets 一致。
"""


def register_get_pet_tool(server: MCPServer, client: PetHospitalClient) -> None:
    """把 ``get_pet`` 注册到 MCPServer，schema 与 list_pets 风格一致。"""

    async def get_pet(pet_id: Any = None) -> CallToolResult:
        return await execute_get_pet(client, pet_id)

    server.add_tool(get_pet, name="get_pet", description=GET_PET_DESCRIPTION)
    registered = server._tool_manager.get_tool("get_pet")  # noqa: SLF001
    assert registered is not None
    registered.parameters = GetPetInput.model_json_schema()
