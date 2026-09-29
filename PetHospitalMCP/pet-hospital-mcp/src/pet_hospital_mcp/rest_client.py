"""Go 宠物医院 REST API 的异步 HTTP 客户端。

职责：
- 只通过 HTTP 访问上游 Go 服务（本服务不持有任何业务数据）；
- 白名单转发 ``GET /api/v1/pets`` 的 14 个查询参数；
- 每次请求有超时；对连接异常 / 超时 / 5xx 做有限重试（线性退避）；
- 把 httpx 异常与上游异常响应分类转换为 :mod:`pet_hospital_mcp.errors`
  中的统一错误，绝不向上抛出底层库异常或堆栈。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from .errors import (
    BackendAPIError,
    BackendInvalidResponseError,
    BackendTimeoutError,
    BackendUnavailableError,
)
from .logging_config import get_logger, redact_sensitive

logger = get_logger(__name__)

#: GET /api/v1/pets 允许转发的全部查询参数（与 Go buildQuery 一一对应）
PETS_QUERY_PARAMS: tuple[str, ...] = (
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

PETS_PATH = "/api/v1/pets"
_PARAM_ALLOWED = frozenset(PETS_QUERY_PARAMS)


def build_pets_query(params: Mapping[str, Any]) -> dict[str, str]:
    """把已校验参数转换成 HTTP query dict。

    - 仅保留白名单内的键；
    - 值为 ``None`` 的键不转发；
    - 不新增任何适配器私有参数；
    - bool 不是合法入参（严格输入模型已拒绝），这里也不做隐式转换。
    """
    query: dict[str, str] = {}
    for key in PETS_QUERY_PARAMS:
        if key not in params:
            continue
        value = params[key]
        if value is None:
            continue
        if isinstance(value, bool):  # pragma: no cover - 严格输入模型已拦截
            raise TypeError(f"查询参数 {key} 不能是布尔值")
        query[key] = str(value)
    # 防御：白名单外参数绝不转发（严格输入模型 extra=forbid 已在更上层拦截）
    unknown = set(params) - _PARAM_ALLOWED
    if unknown:  # pragma: no cover - 严格输入模型已拦截
        raise TypeError(f"不支持的查询参数：{sorted(unknown)}")
    return query


def _extract_upstream_message(response: httpx.Response) -> str | None:
    """尽力读取 Go 错误响应 ``{"message": "..."}`` 中的可读信息。"""
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


class PetHospitalClient:
    """Go REST API 异步客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        max_retries: int = 2,
        retry_backoff: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Args:
            base_url: Go 服务根地址。
            timeout: 单次请求超时秒数。
            max_retries: 首次请求之外的最大重试次数（仅对超时/连接错误/5xx）。
            retry_backoff: 线性退避基数，第 n 次重试等待 ``n * retry_backoff`` 秒。
            transport: 可选自定义 httpx transport（测试注入 MockTransport）。
        """
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PetHospitalClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _sleep_before_retry(self, attempt: int) -> None:
        if self._retry_backoff > 0:
            await asyncio.sleep(attempt * self._retry_backoff)

    async def list_pets(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """调用 ``GET /api/v1/pets``。

        Args:
            params: 已经过严格输入模型校验的参数（14 个白名单键的子集）。

        Returns:
            Go 成功响应的完整 JSON 信封（``code/message/data/time``），
            ``data`` 是否符合数据模型由工具层用 Pydantic 再次校验。

        Raises:
            BackendTimeoutError: 重试后仍超时。
            BackendUnavailableError: 重试后仍无法连接上游。
            BackendAPIError: 上游返回 4xx，或重试耗尽后仍 5xx。
            BackendInvalidResponseError: 上游响应不是合法 JSON 对象。
        """
        query = build_pets_query(params)
        attempts = self._max_retries + 1
        logger.debug("upstream request start", extra={"event_fields": {"params": redact_sensitive(query)}})

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.get(PETS_PATH, params=query)
            except httpx.TimeoutException as exc:
                logger.debug(
                    "upstream timeout",
                    extra={"event_fields": {"attempt": attempt, "max_attempts": attempts}},
                )
                if attempt < attempts:
                    await self._sleep_before_retry(attempt)
                    continue
                raise BackendTimeoutError(
                    "宠物医院后端响应超时，请稍后重试。",
                    details={"attempts": attempt},
                ) from exc
            except httpx.TransportError as exc:
                logger.debug(
                    "upstream unavailable",
                    extra={"event_fields": {"attempt": attempt, "max_attempts": attempts}},
                )
                if attempt < attempts:
                    await self._sleep_before_retry(attempt)
                    continue
                raise BackendUnavailableError(
                    "无法连接宠物医院后端服务，请确认 Go 服务已启动。",
                    details={"attempts": attempt},
                ) from exc

            # 5xx 在还有重试机会时重试
            if response.status_code >= 500 and attempt < attempts:
                logger.debug(
                    "upstream 5xx, retrying",
                    extra={"event_fields": {"attempt": attempt, "http_status": response.status_code}},
                )
                await self._sleep_before_retry(attempt)
                continue

            if response.is_error:
                upstream_message = _extract_upstream_message(response)
                details: dict[str, Any] = {"http_status": response.status_code}
                message = f"宠物医院后端返回错误（HTTP {response.status_code}）。"
                if upstream_message:
                    details["upstream_message"] = upstream_message
                    message = f"宠物医院后端返回错误：{upstream_message}"
                raise BackendAPIError(message, details=details)

            try:
                payload = response.json()
            except ValueError as exc:
                raise BackendInvalidResponseError(
                    "宠物医院后端返回了无法解析的 JSON 响应。",
                    details={"reason": "invalid_json"},
                ) from exc

            if not isinstance(payload, dict):
                raise BackendInvalidResponseError(
                    "宠物医院后端响应结构不符合约定（顶层不是 JSON 对象）。",
                    details={"reason": "not_object"},
                )

            logger.debug(
                "upstream request ok",
                extra={"event_fields": {"attempts": attempt, "http_status": response.status_code}},
            )
            return payload

        # 理论不可达：循环内要么返回、要么抛错
        raise BackendUnavailableError("宠物医院后端暂时不可用。")  # pragma: no cover
