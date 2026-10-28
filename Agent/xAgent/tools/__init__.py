"""工具层:提供默认工具集,供 Agent 通过注册表使用。"""

from .registry import ToolRegistry, ToolSpec
from . import cli_tools, datetime_tools, file_tools, skill_tools, web_tools

__all__ = ["ToolRegistry", "ToolSpec", "build_default_registry"]


def build_default_registry(search_config=None, skill_registry=None) -> ToolRegistry:
    """构建内置工具集(日期时间 + 文件操作 + 网络 + 本地命令 + Skill 加载)。

    search_config / skill_registry 分别为可选的搜索与 Skill 注册表;
    为 None 时对应工具会向 LLM 返回"未启用"提示,而不是让应用启动失败。
    """
    registry = ToolRegistry()
    datetime_tools.register(registry)
    file_tools.register(registry)
    web_tools.register(registry, search_config)
    cli_tools.register(registry)
    skill_tools.register(registry, skill_registry)
    return registry
