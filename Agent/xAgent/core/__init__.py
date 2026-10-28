"""xAgent 核心层:配置、LLM 客户端、事件协议、Agent 编排。

依赖规则:UI 层 -> core 层 -> tools 层,core 不依赖任何具体界面。
"""

from .agent import Agent
from .config import AppConfig, LLMConfig, SearchConfig, UIConfig
from .events import (
    AgentEvent,
    ContentDelta,
    ToolCallStarted,
    ToolResult,
    TurnComplete,
    TurnError,
)
from .skills import SkillMeta, SkillRegistry

__all__ = [
    "Agent",
    "AppConfig",
    "LLMConfig",
    "SearchConfig",
    "UIConfig",
    "SkillMeta",
    "SkillRegistry",
    "AgentEvent",
    "ContentDelta",
    "ToolCallStarted",
    "ToolResult",
    "TurnComplete",
    "TurnError",
]
