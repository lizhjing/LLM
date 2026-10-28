"""界面层统一契约。

新增界面(Console / TUI / Web / Desktop)时,只需实现 BaseUI.run(),
并在 ui/__init__.py 的 create_ui 中注册,core 与 tools 无需任何改动。
"""

from core.agent import Agent


class BaseUI:
    """所有界面的基类;界面通过组合方式持有 Agent,只消费事件流。

    不使用 ABCMeta,避免与 Textual 等 UI 框架的元类冲突。
    """

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def run(self) -> None:
        """启动界面并进入交互循环。"""
        raise NotImplementedError("子类必须实现 run()")
