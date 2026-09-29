"""日志脱敏与结构化字段测试。"""

from __future__ import annotations

import json
import logging
import time

from pet_hospital_mcp.logging_config import (
    configure_logging,
    get_logger,
    log_tool_event,
    redact_sensitive,
)


def test_http_client_loggers_are_silenced() -> None:
    """httpx 的请求行可能在 URL query 中明文打印 ownerPhone，必须压制。"""
    configure_logging()
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_redact_sensitive_both_casings_and_nested() -> None:
    payload = {
        "ownerPhone": "13800000000",
        "owner_phone": "13900000000",
        "ownerAddr": "秘密地址",
        "owner_addr": "秘密地址2",
        "chipNo": "CHIP-1",
        "chip_no": "CHIP-2",
        "nested": [
            {"ownerPhone": "13700000000", "ok": "keep"},
            [{"chipNo": "CHIP-3"}],
        ],
        "species": "犬",
        "keep": {"phoneNote": "非敏感键名应保留"},
    }
    redacted = redact_sensitive(payload)

    assert redacted["ownerPhone"] == "***"
    assert redacted["owner_phone"] == "***"
    assert redacted["ownerAddr"] == "***"
    assert redacted["owner_addr"] == "***"
    assert redacted["chipNo"] == "***"
    assert redacted["chip_no"] == "***"
    assert redacted["nested"][0]["ownerPhone"] == "***"
    assert redacted["nested"][0]["ok"] == "keep"
    assert redacted["nested"][1][0]["chipNo"] == "***"
    # 非敏感字段不受影响
    assert redacted["species"] == "犬"
    assert redacted["keep"]["phoneNote"] == "非敏感键名应保留"


def test_log_tool_event_has_required_fields_and_redacts() -> None:
    configure_logging()
    logger = get_logger("test_tool_event")
    records: list[logging.LogRecord] = []

    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        log_tool_event(
            logger,
            tool_name="list_pets",
            params={
                "ownerPhone": "13800000000",
                "ownerAddr": "某小区",
                "chipNo": "CHIP-X",
                "species": "犬",
                "page": 2,
            },
            status="success",
            started_at=time.perf_counter(),
            total=1,
        )
    finally:
        logger.removeHandler(handler)

    assert len(records) == 1
    from pet_hospital_mcp.logging_config import _JsonFormatter

    data = json.loads(_JsonFormatter().format(records[0]))
    for field in ("timestamp", "tool_name", "params", "status", "duration_ms"):
        assert field in data
    assert data["tool_name"] == "list_pets"
    assert data["status"] == "success"
    assert isinstance(data["duration_ms"], (int, float))
    params = json.loads(data["params"])
    assert params["ownerPhone"] == "***"
    assert params["ownerAddr"] == "***"
    assert params["chipNo"] == "***"
    assert params["species"] == "犬"
    assert params["page"] == 2
