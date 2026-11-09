"""工具注册表:统一管理工具定义(schema)与执行入口。"""

import concurrent.futures
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 工具处理函数:接收解析后的参数字典,返回文本结果
ToolHandler = Callable[[dict[str, Any]], str]

# 工具返回文本的统一截断上限:文件/网页/MCP/CLI 输出共用同一约定,
# 单点维护(各模块的截断提示文案可自行措辞)。
# 默认 2000;启动时由 [tools] result_limit 经 set_text_result_limit 调整,
# 各工具模块在调用点通过 text_result_limit() 读取(勿在导入期缓存副本)
TEXT_RESULT_LIMIT = 2000


def set_text_result_limit(value: int) -> None:
    """按配置调整工具返回文本截断上限(启动阶段调用一次)。"""
    global TEXT_RESULT_LIMIT
    TEXT_RESULT_LIMIT = max(200, int(value))


def text_result_limit() -> int:
    """读取当前截断上限(运行时调用,保证配置生效)。"""
    return TEXT_RESULT_LIMIT


# 工具执行超时兜底默认值(秒):防止单个 handler 卡死(死循环/网络挂起)阻塞整轮对话;
# RunCli 等自带超时的工具不受影响,此值只约束"完全没有超时意识"的 handler。
# 实际生效值由 ToolRegistry 构造参数注入(来自 [tools] tool_timeout)
_DEFAULT_TOOL_TIMEOUT = 120.0


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的定义。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    timeout: float | None = field(default=None)  # 秒;None 用默认兜底值

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

    def __init__(
        self,
        tool_timeout: float = _DEFAULT_TOOL_TIMEOUT,
        max_workers: int = 4,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._schemas: list[dict[str, Any]] | None = None  # 注册时缓存,避免重复构建
        self._tool_timeout = float(tool_timeout)
        # 工具在独立线程池中执行,与对话主线程隔离;池大小来自 [tools] max_workers
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="tool"
        )

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._tools[spec.name] = spec
        self._schemas = None  # 失效缓存

    @property
    def specs(self) -> list[ToolSpec]:
        """全部工具定义(只读视图,供界面展示工具信息)。"""
        return list(self._tools.values())

    @property
    def schemas(self) -> list[dict[str, Any]]:
        if self._schemas is None:
            self._schemas = [spec.schema() for spec in self._tools.values()]
        return list(self._schemas)

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """执行工具;任何异常都转为文本结果,避免中断整轮对话。

        在独立线程池中执行并施加超时兜底:handler 卡死时返回超时文本,
        对话流程继续。
        """
        spec = self._tools.get(name)
        if spec is None:
            return f"未知工具: {name}"
        timeout = spec.timeout if spec.timeout is not None else self._tool_timeout
        future = self._executor.submit(spec.handler, arguments)
        try:
            return str(future.result(timeout=timeout))
        except Exception as exc:  # 工具内部异常不应中断对话
            # future.result 的等待超时与 handler 自身抛出的 TimeoutError 同类,
            # 用 future.done() 区分:等待超时时 future 尚未完成
            if isinstance(exc, TimeoutError) and not future.done():
                future.cancel()  # 运行中的 handler 无法强杀,只能放弃等待
                logger.warning("工具 %s 执行超时(%.0fs)", name, timeout)
                return (
                    f"工具 {name} 执行超时(超过 {timeout:g} 秒)已放弃等待;"
                    "该工具可能在后台仍在运行,请勿重复调用,可改用其他方式完成"
                )
            logger.warning("工具 %s 执行异常: %s: %s", name, type(exc).__name__, exc)
            return f"工具 {name} 出错({type(exc).__name__}): {exc}"
