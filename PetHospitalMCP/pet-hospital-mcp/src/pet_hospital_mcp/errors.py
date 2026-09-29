"""统一结构化错误约定。

所有工具失败（输入校验失败、上游异常、内部异常）都必须转换成下列统一结构，
绝不把 httpx / Pydantic / SDK / Python 堆栈原样暴露给 MCP 客户端::

    {
      "error": {
        "code": "ERROR_CODE",
        "message": "可读错误信息",
        "details": {}
      }
    }
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------


class ErrorCode:
    """对外暴露的稳定错误码。"""

    #: 工具输入未通过严格校验
    VALIDATION_ERROR = "VALIDATION_ERROR"
    #: 上游请求超时
    BACKEND_TIMEOUT = "BACKEND_TIMEOUT"
    #: 无法连接上游（连接拒绝、DNS、网络中断等）
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    #: 上游返回了明确的 HTTP 错误（4xx/5xx）
    BACKEND_API_ERROR = "BACKEND_API_ERROR"
    #: 上游响应无法解析或不符合约定数据模型
    BACKEND_INVALID_RESPONSE = "BACKEND_INVALID_RESPONSE"
    #: 本服务内部未预期错误（细节不对外暴露）
    INTERNAL_ERROR = "INTERNAL_ERROR"


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造统一错误信封。"""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


class PetHospitalMCPError(Exception):
    """所有可预期业务错误的基类，携带稳定错误码与安全细节。"""

    #: 子类覆盖
    code: str = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_body(self) -> dict[str, Any]:
        """转换成统一错误信封（不含任何内部堆栈）。"""
        return error_body(self.code, self.message, self.details)


class ValidationError(PetHospitalMCPError):
    """工具输入严格校验失败。"""

    code = ErrorCode.VALIDATION_ERROR

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details)


class BackendTimeoutError(PetHospitalMCPError):
    """上游请求超时（含重试后仍超时）。"""

    code = ErrorCode.BACKEND_TIMEOUT


class BackendUnavailableError(PetHospitalMCPError):
    """上游不可达。"""

    code = ErrorCode.BACKEND_UNAVAILABLE


class BackendAPIError(PetHospitalMCPError):
    """上游返回 HTTP 4xx/5xx。"""

    code = ErrorCode.BACKEND_API_ERROR


class BackendInvalidResponseError(PetHospitalMCPError):
    """上游响应非法（非 JSON、缺字段、字段类型不符等）。"""

    code = ErrorCode.BACKEND_INVALID_RESPONSE


class InternalError(PetHospitalMCPError):
    """本服务内部错误。"""

    code = ErrorCode.INTERNAL_ERROR
