"""Agent 编排层:管理多轮历史,驱动 LLM 与工具,输出与 UI 无关的事件流。"""

import json
import logging
from pathlib import Path
from typing import Any, Iterator

from tools.registry import ToolRegistry

from .compaction import (
    DEFAULT_CONTEXT_WINDOW,
    archive_messages,
    build_compact_request,
    estimate_tokens,
)
from .config import AppConfig
from .events import (
    AgentEvent,
    CompactionComplete,
    ContentDelta,
    ContextWarning,
    ToolCallStarted,
    ToolResult,
    TurnComplete,
    TurnError,
)
from .llm_client import LLMClient, LLMResponse, StreamDelta
from .skills import SkillRegistry

logger = logging.getLogger(__name__)


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
        context_dir: Path | None = None,
        agents_file: Path | None = None,
    ) -> None:
        self._config = config
        self._tools = tools
        self._client = client or LLMClient(config.llm)
        self._skills = skills
        self._skills_version = skills.version if skills is not None else 0
        # 编排参数来自 [agent] 配置段(默认值与原硬编码一致)
        self._max_tool_rounds = config.agent.max_tool_rounds
        self._compact_ratio = config.agent.compact_ratio
        self._chars_per_token = config.agent.chars_per_token
        self._messages: list[dict[str, Any]] = []
        self._system_message = self._build_system_message(skills)
        if self._system_message is not None:
            self._messages.append(self._system_message)
        # 项目说明:一次注入,全程复用——仅在会话建立时读取一次,
        # 作为固定前导消息随历史持久化;reset/恢复/压缩时都会重新就位,
        # 后续轮次直接复用同一消息,保证前缀缓存有效。
        # 读取顺序:显式 agents_file > 配置的 agents_md_name > AGENT.md 回退。
        agents_path = agents_file or Path.cwd() / config.agent.agents_md_name
        self._agents_message = self._load_agents_message(
            agents_path, config.agent.agents_md_max_chars
        )
        if self._agents_message is None and agents_path.name != "AGENT.md":
            self._agents_message = self._load_agents_message(
                agents_path.with_name("AGENT.md"), config.agent.agents_md_max_chars
            )
        if self._agents_message is not None:
            self._messages.append(self._agents_message)
        # 上下文压缩状态:归档目录、Token 占用、阈值阻断与压缩进行中标记
        self._context_dir = context_dir or (Path.cwd() / ".agents" / "context")
        self._context_tokens = 0
        self._context_from_api = False  # token 值是否来自 API usage(否则为估算)
        self._context_full = False  # 达到阈值后为 True,阻断新提示词直至压缩成功
        self._compacting = False  # 压缩进行中,拒绝对话轮与重复压缩

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

    # ===== 上下文占用监控 =====

    @property
    def context_tokens(self) -> int:
        """当前上下文 Token 占用(优先 API usage,否则按字符数估算)。"""
        return self._context_tokens

    @property
    def context_window(self) -> int:
        """模型上下文窗口大小;配置为 0(未知)时按默认 128k 处理。"""
        configured = self._config.llm.context_window
        return configured if configured > 0 else DEFAULT_CONTEXT_WINDOW

    @property
    def context_window_is_default(self) -> bool:
        """窗口是否为默认值(配置未提供时界面需显示「默认」)。"""
        return self._config.llm.context_window <= 0

    @property
    def context_blocked(self) -> bool:
        """占用达到阈值,已暂停接受新的普通用户提示词。"""
        return self._context_full

    @property
    def history(self) -> list[dict[str, Any]]:
        """对话历史的只读副本(不含界面,供会话保存/导出)。"""
        return [dict(msg) for msg in self._messages]

    # ===== 固定前导消息(system / AGENTS.md) =====

    def _preamble(self) -> list[dict[str, Any]]:
        """会话固定前导消息:system(含 Skill 列表)与 AGENTS.md 项目说明。

        这些消息在任何历史变动(reset/恢复/压缩/截断)后都必须位于最前,
        且内容在一次进程生命周期内不变,保证 LLM 前缀缓存有效。
        """
        preamble: list[dict[str, Any]] = []
        if self._system_message is not None:
            preamble.append(self._system_message)
        if self._agents_message is not None:
            preamble.append(self._agents_message)
        return preamble

    @staticmethod
    def _load_agents_message(path: Path, max_chars: int) -> dict[str, Any] | None:
        """读取项目目录的 AGENTS.md,构建注入用的 system 消息;不存在或为空返回 None。

        超过 max_chars 时截断,并注明「已截断」,避免单个说明文件占满上下文。
        """
        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None  # 文件不存在(常见)或不可读:静默跳过
        if not content:
            return None
        if len(content) > max_chars:
            content = (
                content[:max_chars]
                + f"\n\n…({path.name} 过长,已截断至前 {max_chars} 字符;完整内容请直接查看文件)"
            )
        return {
            "role": "system",
            "content": (
                f"【项目说明({path.name})】\n\n"
                f"{content}\n\n"
                f"以上内容来自项目根目录的 {path.name},回答与工具调用需遵守其中的约定。"
            ),
        }

    def restore_history(self, messages: list[dict[str, Any]]) -> None:
        """恢复历史消息(供会话加载)。

        丢弃文件中的 system 消息(含旧的 AGENTS.md 注入),保留当前 Agent 的
        固定前导消息(Skill 列表与本次读取的 AGENTS.md 可能已随环境变化);
        其余消息按原顺序回填,之后可直接继续对话。
        """
        restored = self._preamble()
        for msg in messages:
            if msg.get("role") != "system":
                restored.append(dict(msg))
        self._messages.clear()
        self._messages.extend(restored)
        self._recompute_token_estimate()

    def reset(self) -> None:
        """清空对话历史(保留固定前导消息)。"""
        self._messages.clear()
        self._messages.extend(self._preamble())
        self._recompute_token_estimate()

    def _recompute_token_estimate(self) -> None:
        """历史被清空/替换/恢复后,把 token 统计重置为估算值。"""
        self._context_tokens = estimate_tokens(self._messages, self._chars_per_token)
        self._context_from_api = False

    def _sync_skills_if_changed(self) -> None:
        """技能注册表变化(如 createSkill 落盘)时重建 system 消息。

        无需重启:下一轮对话开始前自动生效。
        按对象身份定位旧 system 消息,不会误伤同为 system 角色的 AGENTS.md 消息。
        """
        if self._skills is None or self._skills.version == self._skills_version:
            return
        self._skills_version = self._skills.version
        new = self._build_system_message(self._skills)
        old = self._system_message
        self._system_message = new
        if self._messages and self._messages[0] is old:
            if new is None:
                self._messages.pop(0)
            else:
                self._messages[0] = new
        elif new is not None:
            self._messages.insert(0, new)

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
        lines.append(
            "当用户要求创建新的 Skill(技能)时,使用 `createSkill` 工具在项目的 "
            ".agents/skills/ 目录下创建,不要手工创建文件。"
        )
        return {"role": "system", "content": "\n".join(lines)}

    def chat(self, user_input: str) -> Iterator[AgentEvent]:
        """执行一轮对话,产出事件流。

        出错或生成器被提前关闭(如 UI 中断)时,回滚本轮追加的消息,
        保证历史始终合法:不会残留悬空的 assistant(tool_calls) 或孤儿 tool 消息。
        """
        self._sync_skills_if_changed()
        # 压缩期间与阈值阻断期间拒绝新提示词(界面层已先行拦截,此处兜底);
        # 拒绝发生在追加消息之前,历史不受影响
        if self._compacting:
            yield TurnError(message="上下文压缩正在进行中,请等待压缩完成后再发送消息。")
            return
        if self._context_full:
            yield TurnError(
                message="上下文占用已达阈值,已暂停处理新消息。请执行 /compact 压缩上下文后继续。"
            )
            return
        checkpoint = len(self._messages)
        self._messages.append({"role": "user", "content": user_input})
        completed = False
        try:
            yield from self._run_turn()
            self._trim_history()
            completed = True
            # 每轮结束后检查占用;首次达到阈值即警告并进入阻断状态
            yield from self._check_context_limit()
        except Exception as exc:  # 网络/解析等异常统一转为 UI 可展示的事件
            logger.warning("本轮对话异常,回滚 %d 条消息: %s", len(self._messages) - checkpoint, exc)
            del self._messages[checkpoint:]
            yield TurnError(message=str(exc) or exc.__class__.__name__)
        finally:
            # GeneratorExit(BaseException)不走 except 分支,在此兜底回滚;
            # except 分支已回滚时该删除为空切片,幂等无害
            if not completed:
                del self._messages[checkpoint:]

    def _check_context_limit(self) -> Iterator[AgentEvent]:
        """轮次结束后检查上下文占用;首次达到阈值时警告并进入阻断状态。"""
        if self._context_full:
            return
        if self._context_tokens < self.context_window * self._compact_ratio:
            return
        self._context_full = True
        percent = round(self._context_tokens / self.context_window * 100)
        yield ContextWarning(
            message=(
                f"上下文占用已达约 {percent}%,接近模型窗口上限。"
                "请执行 /compact 压缩上下文;压缩完成前将暂停处理新的消息。"
            )
        )

    # ===== 内部实现 =====

    def _run_turn(self) -> Iterator[AgentEvent]:
        # 循环式工具调用:每轮都携带工具定义,模型不再调用工具时本轮结束。
        # 这样模型可在工具结果基础上继续调用其他工具(如加载 Skill 后再取日期)
        for _ in range(self._max_tool_rounds):
            response: LLMResponse | None = None
            for item in self._client.stream_chat(self._messages, self._tools.schemas):
                if isinstance(item, StreamDelta):
                    yield ContentDelta(text=item.text)
                else:
                    response = item
            if response is None:
                raise RuntimeError("LLM 未返回完整响应")
            self._update_context_usage(response)

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
            f"工具调用轮数超过上限 {self._max_tool_rounds},已中止本轮对话"
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

    def _update_context_usage(self, response: LLMResponse) -> None:
        """根据最近一次 LLM 响应更新上下文 Token 占用。

        优先采用 API 返回的 prompt_tokens(天然包含 system、历史与工具消息);
        API 未提供时退回字符数估算。一旦拿到过 API 统计就固定采信 API
        (同一服务端行为一致),避免估算值与真实值来回跳动。
        """
        if response.prompt_tokens:
            self._context_tokens = response.prompt_tokens
            self._context_from_api = True
        elif not self._context_from_api:
            self._context_tokens = estimate_tokens(self._messages, self._chars_per_token)

    # ===== 上下文压缩(/compact) =====

    def compact(self) -> Iterator[AgentEvent]:
        """压缩上下文:归档原始消息 → LLM 生成摘要 → 用摘要替换历史。

        步骤顺序是硬约束:归档写入成功后才允许调用 LLM 与替换历史;
        归档或摘要任一失败都保留原有上下文,可重新执行 /compact。
        压缩请求不带工具定义,期间 _compacting 为 True、新对话轮被拒绝,
        满足「压缩期间禁止 Function Calling、不处理其他任务」的要求。
        """
        if self._compacting:
            yield TurnError(message="上下文压缩正在进行中,请勿重复触发。")
            return
        self._compacting = True
        try:
            yield from self._do_compact()
        finally:
            self._compacting = False

    def _do_compact(self) -> Iterator[AgentEvent]:
        # 1. 归档原始上下文(含 system 消息),失败则中止且不改动任何消息
        try:
            archive_id, archived_at = archive_messages(self._messages, self._context_dir)
        except OSError as exc:
            logger.warning("上下文归档失败: %s", exc)
            yield TurnError(message=f"原始上下文归档失败,已保留现有会话: {exc}")
            return

        # 2. 调用 LLM 生成摘要(流式;不带工具定义)
        summary_parts: list[str] = []
        try:
            response: LLMResponse | None = None
            for item in self._client.stream_chat(build_compact_request(self._messages)):
                if isinstance(item, StreamDelta):
                    summary_parts.append(item.text)
                    yield ContentDelta(text=item.text)
                else:
                    response = item
            summary = "".join(summary_parts)
            if response is None or not summary.strip():
                raise RuntimeError("LLM 未返回有效的压缩摘要")
        except Exception as exc:
            logger.warning("上下文压缩失败: %s", exc)
            yield TurnError(
                message=f"上下文压缩失败,已保留原有上下文,可重新执行 /compact: {exc}"
            )
            return

        # 3. 用「固定前导消息 + 摘要」替换历史;摘要末尾追加归档说明
        note = (
            f"\n\n---\n此前上下文已于 {archived_at} 完成压缩,"
            f"原始上下文保存在编号为 {archive_id} 的归档文件中。"
            "如需查找历史信息,请使用 recallContext 工具。"
        )
        summary_message = {
            "role": "user",
            "content": f"【上下文摘要(由压缩生成)】\n\n{summary}{note}",
        }
        replaced = [*self._preamble(), summary_message]
        self._messages = replaced
        self._recompute_token_estimate()
        self._context_full = False  # 压缩成功,解除输入限制
        logger.info(
            "上下文压缩完成: 归档 %s,摘要约 %d tokens(估算)",
            archive_id,
            self._context_tokens,
        )
        yield CompactionComplete(archive_id=archive_id, summary=summary)

    def _trim_history(self) -> None:
        """按条数上限截断历史。

        约束:恒保留固定前导消息(system/AGENTS.md);不从 tool 消息处截起
        (否则其归属的 assistant(tool_calls) 被切走,形成 API 会拒绝的非法序列)。
        """
        limit = self._config.ui.max_history
        if len(self._messages) <= limit:
            return
        start = len(self._messages) - limit
        while (
            start < len(self._messages)
            and self._messages[start].get("role") == "tool"
        ):
            start += 1
        trimmed = self._messages[start:]
        # 前导消息被截走时(窗口极小)补回;按对象身份识别,避免误删同内容消息
        preamble = self._preamble()
        preamble_ids = {id(msg) for msg in preamble}
        while trimmed and id(trimmed[0]) in preamble_ids:
            trimmed.pop(0)
        self._messages = [*preamble, *trimmed]
