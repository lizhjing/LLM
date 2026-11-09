"""日志体系:滚动文件写入平台日志目录,不向终端输出(避免污染 TUI)。

供诊断凭据读写失败、MCP 连接/重建、LLM 重试、工具执行异常等生产问题;
日志位置:Windows %LOCALAPPDATA%\\xAgent\\logs\\xagent.log,其余平台
~/.local/state/xAgent/logs/xagent.log(1MB×4 轮转)。
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def log_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home()
        return Path(base) / "xAgent" / "logs"
    return Path.home() / ".local" / "state" / "xAgent" / "logs"


def setup_logging() -> Path:
    """初始化根日志器(幂等);返回日志目录。"""
    target_dir = log_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return target_dir  # 已初始化(如测试中重复调用)
    handler = logging.handlers.RotatingFileHandler(
        target_dir / "xagent.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return target_dir
