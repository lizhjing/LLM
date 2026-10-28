"""Agent 编排层:管理多轮历史,驱动 LLM 与工具,输出与 UI 无关的事件流。"""

import json
from typing import Any, Iterator

from tools.registry import ToolRegistry

from .config import AppConfig
from .events import (
    AgentEvent,
    ContentDelta,
    ToolCallStarted,
    ToolResult,
    TurnComplete,
    TurnError,
)
from .llm_client import LLMClient, LLMResponse, StreamDelta
from .skills import SkillRegistry


class Agent:
    """一个可复用的对话 Agent。

    使用方式:
        agent = Agent(config, tool_registry)
        for event in agent.chat("现在几点?"):
            ...  # 界面层自行渲染事件
    """

    def __init__(
        self,
        config: AppConfig,
        tools: ToolRegistry,
        client: LLMClient | None = None,
        skills: SkillRegistry | None = None,
    ) -> None:
        self._config = config
        self._tools = tools
        self._client = client or LLMClient(config.llm)
        self._skills = skills
        self._messages: list[dict[str, Any]] = []
        self._system_message = self._build_system_message(skills)
        if self._system_message is not None:
            self._messages.append(self._system_message)

    @property
    def skills(self) -> SkillRegistry | None:
        """Skill 注册表(只读,供界面展示技能信息)。"""
        return self._skills

    @property
    def tool_registry(self) -> ToolRegistry:
        """工具注册表(只读,供界面展示工具信息)。"""
        return self._tools

    @property
    def model_name(self) -> str:
        """当前使用的 LLM 模型名(供界面状态栏显示)。"""
        return self._config.llm.model

    @property
    def history(self) -> list[dict[str, Any]]:
        """对话历史的只读副本(不含界面,供会话保存/导出)。"""
        return [dict(msg) for msg in self._messages]

    def restore_history(self, messages: list[dict[str, Any]]) -> None:
        """恢复历史消息(供会话加载)。

        丢弃文件中的 system 消息,保留当前 Agent 的 system 消息
        (Skill 列表等可能已随环境变化);其余消息按原顺序回填,
        之后可直接继续对话。
        """
        restored = []
        if self._system_message is not None:
            restored.append(self._system_message)
        for msg in messages:
            if msg.get("role") != "system":
                restored.append(dict(msg))
        self._messages.clear()
        self._messages.extend(restored)

    def reset(self) -> None:
        """清空对话历史(保留 system 消息)。"""
        self._messages.clear()
        if self._system_message is not None:
            self._messages.append(self._system_message)

    @staticmethod
    def _build_system_message(skills: SkillRegistry | None) -> dict[str, Any] | None:
        """构建 system 消息;存在可用 Skill 时列出 name 与 description。

        遵循渐进式披露:此处只放元信息,完整正文由 LLM 通过 loadSkill 按需加载。
        """
        if skills is None or len(skills) == 0:
            return None
        lines = [
            "你是 xAgent,一个可以使用本地工具的智能助手。",
            "",
            "You have access to the following skills:",
        ]
        lines.extend(f"- `{meta.name}`: {meta.description}" for meta in skills.metas)
        lines.append(
            "当用户任务与某个 Skill 的描述匹配时,使用 `loadSkill` 工具加载其完整指引,"
            "再严格按其指引完成任务;需要读取 Skill 引用的附加文件时,使用 `loadSkill` "
            "工具并传入 `filename` 参数。没有匹配的 Skill 时正常作答。"
        )
        return {"role": "system", "content": "\n".join(lines)}

    def chat(self, user_input: str) -> Iterator[AgentEvent]:
        """执行一轮对话,产出事件流。

        出错时回滚本轮追加的消息,保证历史始终是合法且连续的。
        """
        checkpoint = len(self._messages)
        self._messages.append({"role": "user", "content": user_input})
        try:
            yield from self._run_turn()
            self._trim_history()
        except Exception as exc:  # 网络/解析等异常统一转为 UI 可展示的事件
            del self._messages[checkpoint:]
            yield TurnError(message=str(exc) or exc.__class__.__name__)

    # ===== 内部实现 =====

    _MAX_TOOL_ROUNDS = 8  # 单轮对话的工具调用轮数上限,防止模型陷入循环

    def _run_turn(self) -> Iterator[AgentEvent]:
        # 循环式工具调用:每轮都携带工具定义,模型不再调用工具时本轮结束。
        # 这样模型可在工具结果基础上继续调用其他工具(如加载 Skill 后再取日期)
        for _ in range(self._MAX_TOOL_ROUNDS):
            response: LLMResponse | None = None
            for item in self._client.stream_chat(self._messages, self._tools.schemas):
                if isinstance(item, StreamDelta):
                    yield ContentDelta(text=item.text)
                else:
                    response = item
            if response is None:
                raise RuntimeError("LLM 未返回完整响应")

            # 模型没有调用工具:本轮直接结束
            if not response.tool_calls:
                self._messages.append({"role": "assistant", "content": response.content})
                yield TurnComplete(content=response.content)
                return

            # 记录 assistant 的 tool_calls(第三方接口要求原样回填)
            self._messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments or "{}",
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )

            # 逐个执行工具,并把结果回填为 tool 消息
            for call in response.tool_calls:
                name = call.name or ""
                arguments = self._parse_arguments(call.arguments)
                yield ToolCallStarted(name=name, arguments=arguments)

                result = self._tools.execute(name, arguments)
                yield ToolResult(name=name, result=result)
                self._messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )

        raise RuntimeError(
            f"工具调用轮数超过上限 {self._MAX_TOOL_ROUNDS},已中止本轮对话"
        )

    @staticmethod
    def _parse_arguments(raw: str | None) -> dict[str, Any]:
        """解析工具参数;非法 JSON 返回空字典,由工具层给出校验提示。"""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _trim_history(self) -> None:
        """按条数上限截断历史,与原有行为保持一致。"""
        limit = self._config.ui.max_history
        if len(self._messages) > limit:
            self._messages = self._messages[-limit:]
