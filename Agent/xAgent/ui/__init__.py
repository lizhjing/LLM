"""界面工厂:所有可用界面的唯一注册点。"""

from core.agent import Agent
from core.config import AppConfig

from .base import BaseUI

# 已实现的界面;未来新增 web / desktop 时追加即可
AVAILABLE_UIS = ("tui", "console")


def available_uis() -> list[str]:
    return list(AVAILABLE_UIS)


def create_ui(
    name: str,
    agent: Agent,
    config: AppConfig,
    *,
    mcp_loader=None,
    mcp_conns_sink: list | None = None,
) -> BaseUI:
    """按名称创建界面实例。

    延迟导入具体界面,避免未安装 Textual 时影响 Console 界面启动。
    mcp_loader / mcp_conns_sink 仅 TUI 使用:前者在后台线程异步连接
    MCP Server,后者承接连接对象供 main 在退出时统一关停;
    Console 界面的 MCP 在 main 中同步加载,无需这两个参数。
    """
    if name == "console":
        from .console import ConsoleUI

        return ConsoleUI(agent, typewriter_delay=config.ui.typewriter_delay)

    if name == "tui":
        from .tui import XAgentTUI

        return XAgentTUI(
            agent, mcp_loader=mcp_loader, mcp_conns_sink=mcp_conns_sink
        )

    raise ValueError(f"未知界面类型: {name}(可选: {', '.join(AVAILABLE_UIS)})")
