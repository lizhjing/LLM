"""UI 无关的事件协议。

Agent 把一轮对话的过程拆成事件流输出,任何界面(Console / TUI / Web / Desktop)
只需消费这些事件即可渲染,不需要了解 LLM 与工具的内部细节。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    """所有 Agent 事件的基类。"""


@dataclass(frozen=True)
class ContentDelta(AgentEvent):
    """模型流式输出的一个文本片段。"""

    text: str


@dataclass(frozen=True)
class ToolCallStarted(AgentEvent):
    """模型请求调用工具,已解析出参数,即将执行。"""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult(AgentEvent):
    """工具执行完成,返回结果字符串。"""

    name: str
    result: str


@dataclass(frozen=True)
class TurnComplete(AgentEvent):
    """本轮对话结束,content 为最终回答全文。"""

    content: str


@dataclass(frozen=True)
class TurnError(AgentEvent):
    """本轮对话失败(网络异常、响应解析失败等),历史已回滚。"""

    message: str


@dataclass(frozen=True)
class ContextWarning(AgentEvent):
    """上下文占用达到阈值(本轮对话本身已正常完成)。

    提示用户执行 /compact;此后 Agent 暂停接受新的普通用户提示词,
    直到压缩成功完成。
    """

    message: str


@dataclass(frozen=True)
class CompactionComplete(AgentEvent):
    """上下文压缩完成,历史已被摘要替换。"""

    archive_id: str  # 原始上下文归档编号,可用 recallContext 检索
    summary: str  # 压缩摘要全文(含归档说明)
