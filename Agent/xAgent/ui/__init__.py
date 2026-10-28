"""界面工厂:所有可用界面的唯一注册点。"""

from core.agent import Agent
from core.config import AppConfig

from .base import BaseUI

# 已实现的界面;未来新增 web / desktop 时追加即可
AVAILABLE_UIS = ("tui", "console")


def available_uis() -> list[str]:
    return list(AVAILABLE_UIS)


def create_ui(name: str, agent: Agent, config: AppConfig) -> BaseUI:
    """按名称创建界面实例。

    延迟导入具体界面,避免未安装 Textual 时影响 Console 界面启动。
    """
    if name == "console":
        from .console import ConsoleUI

        return ConsoleUI(agent, typewriter_delay=config.ui.typewriter_delay)

    if name == "tui":
        from .tui import XAgentTUI

        return XAgentTUI(agent)

    raise ValueError(f"未知界面类型: {name}(可选: {', '.join(AVAILABLE_UIS)})")
