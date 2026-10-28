"""工具注册表:统一管理工具定义(schema)与执行入口。"""

from dataclasses import dataclass
from typing import Any, Callable

# 工具处理函数:接收解析后的参数字典,返回文本结果
ToolHandler = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的定义。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        """转为 OpenAI Function Calling 所需的工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具注册与执行的统一入口,UI 与 Agent 都不直接接触具体工具实现。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._tools[spec.name] = spec

    @property
    def specs(self) -> list[ToolSpec]:
        """全部工具定义(只读视图,供界面展示工具信息)。"""
        return list(self._tools.values())

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema() for spec in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """执行工具;任何异常都转为文本结果,避免中断整轮对话。"""
        spec = self._tools.get(name)
        if spec is None:
            return f"未知工具: {name}"
        try:
            return str(spec.handler(arguments))
        except Exception as exc:  # 工具内部异常不应中断对话
            return f"工具执行出错: {exc}"
