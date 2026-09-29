"""结构化 JSON 日志与敏感信息脱敏。

每条工具调用日志至少包含：``timestamp``、``tool_name``、``params``、
``status``、``duration_ms``。

脱敏规则：对下列键（camelCase 与 snake_case 两种写法，大小写不敏感）在任意
嵌套层级的值替换为 ``"***"``：``ownerPhone``、``ownerAddr``、``chipNo``。
日志只记录入参与执行状态/耗时，不记录完整业务返回数据。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

#: 需要递归脱敏的字段名（统一用小写比较，同时覆盖 camelCase / snake_case）
SENSITIVE_KEYS = frozenset(
    {
        "ownerphone",
        "owner_phone",
        "owneraddr",
        "owner_addr",
        "chipno",
        "chip_no",
    }
)

REDACTED = "***"

#: 工具日志中 params 的最大渲染长度，避免把大段业务数据写入日志
MAX_PARAMS_CHARS = 2000


def redact_sensitive(value: Any) -> Any:
    """递归遍历 dict / list / tuple，对敏感键的值脱敏。

    其他类型原样返回；字典键若不是字符串也原样保留。
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value


class _JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "event_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


_CONFIGURED_MARK = "pet_hospital_mcp_json_handler"


def configure_logging(level: str | int = logging.INFO) -> None:
    """配置包 logger 的 JSON handler（幂等，可重复调用）。"""
    root = logging.getLogger("pet_hospital_mcp")
    if any(getattr(handler, _CONFIGURED_MARK, False) for handler in root.handlers):
        root.setLevel(level)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        setattr(handler, _CONFIGURED_MARK, True)
        root.addHandler(handler)
        root.setLevel(level)
        root.propagate = False

    # httpx/httpcore 自带的 "HTTP Request: GET ...?ownerPhone=..." INFO 日志
    # 会在 URL query 中泄露 ownerPhone 等敏感信息，这里统一提升到 WARNING，
    # 使一切对外请求只能通过本模块已脱敏的 JSON 事件日志留痕。
    for noisy_logger in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取带包名前缀的 logger。"""
    if name.startswith("pet_hospital_mcp"):
        return logging.getLogger(name)
    return logging.getLogger(f"pet_hospital_mcp.{name}")


def _truncate(text: str) -> str:
    if len(text) <= MAX_PARAMS_CHARS:
        return text
    return text[:MAX_PARAMS_CHARS] + "...<truncated>"


def log_tool_event(
    logger: logging.Logger,
    *,
    tool_name: str,
    params: Any,
    status: str,
    started_at: float,
    **extra: Any,
) -> None:
    """记录一次工具调用事件。

    Args:
        tool_name: 工具名（snake_case）。
        params: 原始入参，记录前会递归脱敏并截断。
        status: 调用结果状态，如 ``success`` / ``validation_error`` /
            ``backend_timeout`` 等。
        started_at: 由 :func:`time.perf_counter` 取得的开始时间戳。
        **extra: 追加字段（不得包含完整业务数据）。
    """
    safe_params = redact_sensitive(params)
    try:
        rendered = json.dumps(safe_params, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 兜底，default=str 已覆盖大多数情况
        rendered = repr(safe_params)

    fields: dict[str, Any] = {
        "tool_name": tool_name,
        "params": _truncate(rendered),
        "status": status,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
    }
    fields.update(extra)
    logger.info("tool_call", extra={"event_fields": fields})
