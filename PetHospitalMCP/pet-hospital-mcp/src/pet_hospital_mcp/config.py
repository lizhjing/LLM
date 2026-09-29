"""运行配置。

所有配置均来自环境变量（12-Factor 风格），教学场景下不读取配置文件：

``PET_HOSPITAL_BASE_URL``
    Go 宠物医院 REST API 根地址，默认 ``http://127.0.0.1:8080``。
``MCP_HOST`` / ``MCP_PORT``
    MCP 服务监听地址，默认 ``127.0.0.1:8000``。
``PET_HOSPITAL_TIMEOUT``
    单次上游 HTTP 请求超时秒数，默认 5.0。
``PET_HOSPITAL_MAX_RETRIES``
    上游失败后的额外重试次数（0 = 不重试），默认 2。
``PET_HOSPITAL_RETRY_BACKOFF``
    重试退避基数秒数（线性退避：``attempt * backoff``），默认 0.2。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Go REST API 默认根地址
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
#: MCP 服务默认监听地址（教学场景默认只绑定本机回环）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
#: 上游调用默认参数
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.2


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {key} 必须是整数，实际收到 {raw!r}") from exc


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {key} 必须是数字，实际收到 {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """不可变运行配置。"""

    #: Go REST API 根地址（无尾部斜杠）
    base_url: str
    #: MCP 监听主机
    host: str
    #: MCP 监听端口
    port: int
    #: 单次上游请求超时（秒）
    request_timeout: float
    #: 上游失败后的最大重试次数（不含首次）
    max_retries: int
    #: 重试退避基数（秒）
    retry_backoff: float


def load_settings() -> Settings:
    """从环境变量加载并校验配置。

    Raises:
        ValueError: 数值型配置非法或超出合理范围。
    """
    settings = Settings(
        base_url=_env_str("PET_HOSPITAL_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        host=_env_str("MCP_HOST", DEFAULT_HOST),
        port=_env_int("MCP_PORT", DEFAULT_PORT),
        request_timeout=_env_float("PET_HOSPITAL_TIMEOUT", DEFAULT_TIMEOUT_SECONDS),
        max_retries=_env_int("PET_HOSPITAL_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        retry_backoff=_env_float("PET_HOSPITAL_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF_SECONDS),
    )

    if not (1 <= settings.port <= 65535):
        raise ValueError(f"MCP_PORT 必须在 1-65535 之间，实际为 {settings.port}")
    if settings.request_timeout <= 0:
        raise ValueError(f"PET_HOSPITAL_TIMEOUT 必须大于 0，实际为 {settings.request_timeout}")
    if settings.max_retries < 0:
        raise ValueError(f"PET_HOSPITAL_MAX_RETRIES 不能为负数，实际为 {settings.max_retries}")
    if settings.retry_backoff < 0:
        raise ValueError(f"PET_HOSPITAL_RETRY_BACKOFF 不能为负数，实际为 {settings.retry_backoff}")
    if not settings.base_url.startswith(("http://", "https://")):
        raise ValueError(
            f"PET_HOSPITAL_BASE_URL 必须以 http:// 或 https:// 开头，实际为 {settings.base_url!r}"
        )
    return settings
