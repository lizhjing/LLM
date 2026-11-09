"""工具层:提供默认工具集,供 Agent 通过注册表使用。"""

from typing import TYPE_CHECKING

from .registry import ToolRegistry, ToolSpec, set_text_result_limit
from . import cli_tools, datetime_tools, file_tools, skill_tools, web_tools

if TYPE_CHECKING:  # 仅类型检查引用,避免 tools -> core 的运行时反向依赖
    from core.config import ToolsConfig

__all__ = ["ToolRegistry", "ToolSpec", "build_default_registry"]


def build_default_registry(
    search_config=None,
    skill_registry=None,
    tools_config: "ToolsConfig | None" = None,
) -> ToolRegistry:
    """构建内置工具集(日期时间 + 文件操作 + 网络 + 本地命令 + Skill 加载)。

    search_config / skill_registry 分别为可选的搜索与 Skill 注册表;
    为 None 时对应工具会向 LLM 返回"未启用"提示,而不是让应用启动失败。
    tools_config 来自 [tools] 配置段;None 时使用 registry 模块默认参数。
    """
    if tools_config is not None:
        set_text_result_limit(tools_config.result_limit)
    registry = ToolRegistry(
        tool_timeout=tools_config.tool_timeout if tools_config else 120.0,
        max_workers=tools_config.max_workers if tools_config else 4,
    )
    datetime_tools.register(registry)
    file_tools.register(registry)
    web_tools.register(registry, search_config)
    cli_tools.register(registry)
    skill_tools.register(registry, skill_registry)
    return registry
