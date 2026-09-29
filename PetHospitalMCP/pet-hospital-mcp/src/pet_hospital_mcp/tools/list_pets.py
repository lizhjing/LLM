"""唯一的 MCP 工具：``list_pets``。

严格适配 Go 后端 ``GET /api/v1/pets``：

- 支持且仅支持 14 个查询参数（见 :data:`LIST_PETS_PARAM_NAMES`）；
- 输入用 :class:`ListPetsInput` 做严格 Pydantic 校验（拒绝未知字段、NaN、
  Infinity 以及类型不正确的值；枚举与取值范围与 Go 后端完全一致）；
- 成功输出对应 Go 成功响应的 ``data``（items/total/page/pageSize/
  totalPages/totalCost），并兼容 Go 中 ``records`` / ``charges`` 为
  ``null`` 或数组的真实 JSON 表现；
- 一切失败均返回统一结构化错误，且 ``CallToolResult.is_error=True``。

关于 SDK 2.x 的装配说明（见 :func:`register_list_pets_tool`）：
``MCPServer`` 根据函数签名自动生成参数模型，其默认模型 ``extra="ignore"``
且会在进入函数体之前抛错，无法满足“拒绝未知字段 / 所有非法输入都返回统一
错误结构”的要求。因此注册函数的形参故意声明为宽松的 ``Any``（保证任何输入
都能进入函数体），再由函数体用 :class:`ListPetsInput` 对 **原始 JSON 参数**
做严格校验；同时把工具对外公布的 JSON Schema 显式替换为严格输入模型的
schema，使 ``tools/list`` 看到的仍是完整枚举与取值约束。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from ..errors import (
    BackendInvalidResponseError,
    ErrorCode,
    InternalError,
    PetHospitalMCPError,
    error_body,
)
from ..logging_config import get_logger, log_tool_event
from ..rest_client import PetHospitalClient

logger = get_logger("tools.list_pets")

# ---------------------------------------------------------------------------
# 与 Go 后端完全一致的枚举 / 取值约束
# ---------------------------------------------------------------------------

#: 物种（model.ValidSpecies）
SPECIES_VALUES: tuple[str, ...] = ("犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他")
#: 就诊状态（model.ValidStatus）
STATUS_VALUES: tuple[str, ...] = ("待就诊", "就诊中", "住院中", "已康复", "慢性病随访")
#: 可排序字段（/api/v1/meta sortFields）
SORT_BY_VALUES: tuple[str, ...] = (
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
)
#: 排序方向
ORDER_VALUES: tuple[str, ...] = ("asc", "desc")

#: 工具支持且仅支持的参数名（与 Go buildQuery 白名单一一对应）
LIST_PETS_PARAM_NAMES: tuple[str, ...] = (
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

Species = Literal["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]
PetStatus = Literal["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]
SortBy = Literal[
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
]
Order = Literal["asc", "desc"]

#: 非负、有限的费用数字（拒绝 NaN / Infinity / 负数）
NonNegativeCost = Annotated[float, Field(ge=0, allow_inf_nan=False)]
#: 页码 >= 1
PageNumber = Annotated[int, Field(ge=1)]
#: 每页条数 1..500（与 store.List 的上限一致）
PageSizeNumber = Annotated[int, Field(ge=1, le=500)]


# ---------------------------------------------------------------------------
# 输入模型（严格）
# ---------------------------------------------------------------------------


class ListPetsInput(BaseModel):
    """``list_pets`` 的严格输入模型。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    q: str | None = Field(default=None, description="全文关键词（宠物名/主人/疾病等，由后端解释）")
    name: str | None = Field(default=None, description="按宠物姓名过滤")
    ownerName: str | None = Field(default=None, description="按主人姓名过滤")
    ownerPhone: str | None = Field(default=None, description="按主人电话过滤")
    species: Species | None = Field(default=None, description="按物种过滤")
    doctor: str | None = Field(default=None, description="按主治医生过滤")
    disease: str | None = Field(default=None, description="按疾病/诊断关键词过滤")
    status: PetStatus | None = Field(default=None, description="按就诊状态过滤")
    min: NonNegativeCost | None = Field(default=None, description="总花费下限（元，非负数），与 max 同时给出时必须 min<=max")
    max: NonNegativeCost | None = Field(default=None, description="总花费上限（元，非负数）")
    sortBy: SortBy | None = Field(default=None, description="排序字段，默认 createdAt")
    order: Order | None = Field(default=None, description="排序方向：asc/desc，默认 desc")
    page: PageNumber | None = Field(default=None, description="页码，从 1 开始，默认 1")
    pageSize: PageSizeNumber | None = Field(default=None, description="每页条数 1-500，默认 20")

    # Pydantic v2 跨字段校验：min <= max
    @model_validator(mode="after")
    def _validate_cost_range(self) -> "ListPetsInput":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min 必须小于或等于 max")
        return self


# ---------------------------------------------------------------------------
# 成功输出模型（对应 Go 成功响应 data；宽松以容忍后端演进）
# ---------------------------------------------------------------------------


class MedicalRecordItem(BaseModel):
    """一次病历（Go MedicalRecord），未建模字段一律允许。"""

    model_config = ConfigDict(extra="allow", strict=False)

    id: str | None = None
    visitDate: str | None = None
    doctor: str | None = None
    diagnosis: str | None = None
    symptoms: str | None = None
    treatment: str | None = None
    prescription: list[str] | None = None
    weightKg: float | None = None
    temperature: float | None = None
    followUp: str | None = None
    charge: float | None = None
    createdAt: str | None = None


class TreatmentItem(BaseModel):
    """一条收费明细（Go Treatment），未建模字段一律允许。"""

    model_config = ConfigDict(extra="allow", strict=False)

    id: str | None = None
    item: str | None = None
    category: str | None = None
    amount: float | None = None
    doctor: str | None = None
    date: str | None = None
    note: str | None = None


class Pet(BaseModel):
    """宠物档案（Go Pet）。

    ``records`` / ``charges`` 在 Go 端为 nil 切片时 JSON 序列化为 ``null``，
    因此这里显式允许 ``None`` 与数组两种形态；后端未来新增字段也予以容忍。
    """

    model_config = ConfigDict(extra="allow", strict=False)

    id: str | None = None
    name: str | None = None
    species: str | None = None
    breed: str | None = None
    gender: str | None = None
    ageMonths: int | None = None
    color: str | None = None
    chipNo: str | None = None
    ownerName: str | None = None
    ownerPhone: str | None = None
    ownerAddr: str | None = None
    doctor: str | None = None
    disease: str | None = None
    status: str | None = None
    allergy: str | None = None
    note: str | None = None
    # 关键：必须兼容 null 或数组
    records: list[MedicalRecordItem] | None = None
    charges: list[TreatmentItem] | None = None
    totalCost: float | None = None
    visitCount: int | None = None
    createdAt: str | None = None
    updatedAt: str | None = None


class ListPetsData(BaseModel):
    """Go ``store.Result`` —— 成功响应 ``data`` 的精确 6 个字段。"""

    model_config = ConfigDict(extra="ignore")

    items: list[Pet]
    total: int
    page: int
    pageSize: int
    totalPages: int
    totalCost: float


class PetsBackendResponse(BaseModel):
    """Go 顶层响应信封（code/message/time 等字段容忍忽略）。"""

    model_config = ConfigDict(extra="allow")

    data: ListPetsData


# ---------------------------------------------------------------------------
# 错误输出模型（仅用于文档化/自检统一错误结构）
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


# ---------------------------------------------------------------------------
# 工具执行核心（不依赖 MCP Context，便于单测）
# ---------------------------------------------------------------------------

#: 错误码 -> 日志 status
_STATUS_BY_CODE = {
    ErrorCode.VALIDATION_ERROR: "validation_error",
    ErrorCode.BACKEND_TIMEOUT: "backend_timeout",
    ErrorCode.BACKEND_UNAVAILABLE: "backend_unavailable",
    ErrorCode.BACKEND_API_ERROR: "backend_api_error",
    ErrorCode.BACKEND_INVALID_RESPONSE: "backend_invalid_response",
    ErrorCode.INTERNAL_ERROR: "internal_error",
}


def _error_result(body: dict[str, Any]) -> CallToolResult:
    text = json.dumps(body, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=body,
        is_error=True,
    )


def _success_result(data: ListPetsData) -> CallToolResult:
    payload = data.model_dump(mode="json", by_alias=True)
    text = json.dumps(payload, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=False,
    )


def _validation_body(exc: PydanticValidationError) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        issues.append(
            {
                "field": loc,
                "type": err.get("type", "value_error"),
                "message": err.get("msg", "参数不合法"),
            }
        )
    return error_body(
        ErrorCode.VALIDATION_ERROR,
        "list_pets 参数校验失败，请检查入参取值与类型。",
        details={"issues": issues},
    )


async def execute_list_pets(
    client: PetHospitalClient,
    raw_arguments: Any,
) -> CallToolResult:
    """执行 list_pets 的纯业务核心。

    Args:
        client: 已配置好超时/重试的 Go REST 客户端。
        raw_arguments: 未经 SDK 转换的原始 JSON 参数对象。

    Returns:
        成功数据或统一错误结构的 :class:`CallToolResult`（绝不向 SDK 抛业务异常）。
    """
    started_at = time.perf_counter()

    if not isinstance(raw_arguments, dict):
        body = error_body(
            ErrorCode.VALIDATION_ERROR,
            "list_pets 参数必须是 JSON 对象。",
            details={"received_type": type(raw_arguments).__name__},
        )
        log_tool_event(
            logger,
            tool_name="list_pets",
            params=raw_arguments,
            status="validation_error",
            started_at=started_at,
        )
        return _error_result(body)

    # 1) 严格输入校验（未知字段/枚举/范围/NaN/Infinity/类型全部在此被拒）
    try:
        model_input = ListPetsInput.model_validate(raw_arguments)
    except PydanticValidationError as exc:
        body = _validation_body(exc)
        log_tool_event(
            logger,
            tool_name="list_pets",
            params=raw_arguments,
            status="validation_error",
            started_at=started_at,
        )
        return _error_result(body)
    except Exception:  # noqa: BLE001 - 校验阶段不应有其他异常，兜底为内部错误
        logger.exception("unexpected error during input validation")
        body = InternalError("服务内部错误。").to_body()
        log_tool_event(
            logger,
            tool_name="list_pets",
            params=raw_arguments,
            status="internal_error",
            started_at=started_at,
        )
        return _error_result(body)

    params = model_input.model_dump(exclude_none=True)

    # 2) 调用 Go 后端并校验响应
    try:
        envelope = await client.list_pets(params)
        try:
            parsed_response = PetsBackendResponse.model_validate(envelope)
        except PydanticValidationError as exc:
            raise BackendInvalidResponseError(
                "宠物医院后端返回的数据结构不符合约定。",
                details={"reason": "data_schema_mismatch"},
            ) from exc
    except PetHospitalMCPError as exc:
        body = exc.to_body()
        log_tool_event(
            logger,
            tool_name="list_pets",
            params=params,
            status=_STATUS_BY_CODE.get(exc.code, "error"),
            started_at=started_at,
            error_code=exc.code,
        )
        return _error_result(body)
    except Exception:  # noqa: BLE001 - 不向客户端泄露任何内部异常/堆栈
        logger.exception("unexpected error calling pet hospital backend")
        body = InternalError("服务内部错误。").to_body()
        log_tool_event(
            logger,
            tool_name="list_pets",
            params=params,
            status="internal_error",
            started_at=started_at,
        )
        return _error_result(body)

    log_tool_event(
        logger,
        tool_name="list_pets",
        params=params,
        status="success",
        started_at=started_at,
        total=parsed_response.data.total,
        total_pages=parsed_response.data.totalPages,
    )
    return _success_result(parsed_response.data)


# ---------------------------------------------------------------------------
# MCPServer 注册
# ---------------------------------------------------------------------------

#: 工具说明（用途 / 参数 / 适用场景 / 返回值）
LIST_PETS_DESCRIPTION = """\
查询宠物医院档案列表（分页、过滤、排序），数据来自 Go 后端 GET /api/v1/pets。

用途：
  按关键词、宠物名、主人姓名/电话、物种、主治医生、疾病、就诊状态、总花费
  区间检索宠物档案，并支持指定排序字段、排序方向与分页。

参数（全部可选，均与 Go REST API 查询参数同名）：
  q           全文关键词
  name        宠物姓名
  ownerName   主人姓名
  ownerPhone  主人电话
  species     物种，允许值：犬/猫/兔/鸟/仓鼠/爬宠/其他
  doctor      主治医生
  disease     疾病/诊断关键词
  status      就诊状态，允许值：待就诊/就诊中/住院中/已康复/慢性病随访
  min         总花费下限（元，非负数）
  max         总花费上限（元，非负数）；min 与 max 同时给出时必须 min<=max
  sortBy      排序字段：id/name/ownerName/species/doctor/disease/status/
              totalCost/visitCount/createdAt/updatedAt（默认 createdAt）
  order       排序方向：asc/desc（默认 desc）
  page        页码，>=1，默认 1
  pageSize    每页条数，1-500，默认 20

适用场景：
  - “查一下张姓主人在治的猫”“最近花了 500 元以上的病历”等检索类问题；
  - 需要分页浏览档案、按消费金额排序等。
  不支持新增/修改病历等写操作。

返回值（成功，structuredContent 即 Go 响应 data）：
  {
    "items": [宠物档案，records/charges 可能为 null 或数组],
    "total": 过滤后总数,
    "page": 当前页,
    "pageSize": 每页条数,
    "totalPages": 总页数,
    "totalCost": 当前结果集花费合计
  }
失败时返回统一错误信封 {error: {code, message, details}}，isError=true；
错误码包括 VALIDATION_ERROR / BACKEND_TIMEOUT / BACKEND_UNAVAILABLE /
BACKEND_API_ERROR / BACKEND_INVALID_RESPONSE / INTERNAL_ERROR。
"""


def register_list_pets_tool(server: MCPServer, client: PetHospitalClient) -> None:
    """把 ``list_pets`` 注册到 MCPServer。

    形参刻意全部声明为 ``Any``：SDK 2.x 自动生成的参数模型默认
    ``extra="ignore"``，且会在函数体执行前拦截校验错误（无法返回统一错误结构）。
    因此所有严格校验都在 :func:`execute_list_pets` 内用
    :class:`ListPetsInput` 完成；注册后再把对外 JSON Schema 替换为严格模型的
    schema，保证 ``tools/list`` 公布的枚举/范围约束完整准确。
    """

    async def list_pets(  # noqa: PLR0913 - 14 个参数与 REST 查询参数一一对应
        q: Any = None,
        name: Any = None,
        ownerName: Any = None,
        ownerPhone: Any = None,
        species: Any = None,
        doctor: Any = None,
        disease: Any = None,
        status: Any = None,
        min: Any = None,
        max: Any = None,
        sortBy: Any = None,
        order: Any = None,
        page: Any = None,
        pageSize: Any = None,
        ctx: Context | None = None,
    ) -> CallToolResult:
        raw_arguments = _extract_raw_arguments(ctx)
        if raw_arguments is None:  # 理论上 HTTP 调用必有；脱离协议直接调用时退回形参
            raw_arguments = {
                key: value
                for key, value in (
                    ("q", q),
                    ("name", name),
                    ("ownerName", ownerName),
                    ("ownerPhone", ownerPhone),
                    ("species", species),
                    ("doctor", doctor),
                    ("disease", disease),
                    ("status", status),
                    ("min", min),
                    ("max", max),
                    ("sortBy", sortBy),
                    ("order", order),
                    ("page", page),
                    ("pageSize", pageSize),
                )
                if value is not None
            }
        return await execute_list_pets(client, raw_arguments)

    server.add_tool(list_pets, name="list_pets", description=LIST_PETS_DESCRIPTION)

    # 用严格输入模型的 JSON Schema 替换 SDK 按 Any 形参生成的宽松 schema。
    registered = server._tool_manager.get_tool("list_pets")  # noqa: SLF001
    assert registered is not None
    registered.parameters = ListPetsInput.model_json_schema()


def _extract_raw_arguments(ctx: Context | None) -> dict[str, Any] | None:
    """从 MCP 请求上下文取出未经转换的原始 arguments。

    两条路径：
    - 类型化信封：``request_context.request.params.arguments``；
    - 2026-07-28 现代无状态路径：类型化 ``request`` 可能为 None，
      此时原始 JSON-RPC params 映射位于 ``request_context.params``，
      其中 ``arguments`` 即未经 SDK 模型过滤的原始入参。
    """
    if ctx is None:
        return None
    try:
        request_context = ctx.request_context
    except Exception:  # noqa: BLE001
        return None

    typed_request = getattr(request_context, "request", None)
    arguments = getattr(getattr(typed_request, "params", None), "arguments", None)
    if isinstance(arguments, dict):
        return arguments

    raw_params = getattr(request_context, "params", None)
    if isinstance(raw_params, Mapping):
        arguments = raw_params.get("arguments")
        if isinstance(arguments, dict):
            return arguments
    return None
