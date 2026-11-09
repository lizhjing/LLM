"""Console UI:命令行界面,消费 Agent 事件流。"""

import time

from core.agent import Agent
from core.events import (
    AgentEvent,
    CompactionComplete,
    ContentDelta,
    ContextWarning,
    ToolCallStarted,
    ToolResult,
    TurnComplete,
    TurnError,
)

from .base import BaseUI


class ConsoleUI(BaseUI):
    """传统终端交互,行为与 Practice02/chat.py 保持一致。"""

    def __init__(self, agent: Agent, typewriter_delay: float = 0.01) -> None:
        super().__init__(agent)
        self._typewriter_delay = typewriter_delay
        self._assistant_open = False

    def run(self) -> None:
        try:
            while True:
                user_input = input("你: ")
                self._assistant_open = False
                for event in self._agent.chat(user_input):
                    self._render(event)
        except (KeyboardInterrupt, EOFError):
            print("\n已退出对话。")

    def _render(self, event: AgentEvent) -> None:
        if isinstance(event, ContentDelta):
            if not self._assistant_open:
                print("AI: ", end="", flush=True)
                self._assistant_open = True
            self._type_out(event.text)
        elif isinstance(event, ToolCallStarted):
            self._close_assistant()
            print(f"[调用工具: {event.name}]")
        elif isinstance(event, ToolResult):
            print(f"[工具结果: {event.result}]")
        elif isinstance(event, TurnComplete):
            self._close_assistant()
        elif isinstance(event, TurnError):
            self._close_assistant()
            print(f"\n[本轮出错,已跳过:{event.message}]")
        elif isinstance(event, ContextWarning):
            self._close_assistant()
            print(f"\n[上下文告警:{event.message}]")
        elif isinstance(event, CompactionComplete):
            self._close_assistant()
            print(f"\n[上下文压缩完成,原始记录已归档为 {event.archive_id}]")

    def _type_out(self, text: str) -> None:
        """打字机效果逐字输出。"""
        for char in text:
            print(char, end="", flush=True)
            if self._typewriter_delay > 0:
                time.sleep(self._typewriter_delay)

    def _close_assistant(self) -> None:
        if self._assistant_open:
            print()
            self._assistant_open = False
