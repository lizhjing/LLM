"""Textual TUI 界面:只负责渲染 Agent 事件,不包含任何 LLM/工具逻辑。"""

import json

from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Markdown, Static

from core.agent import Agent
from core.events import (
    AgentEvent,
    ContentDelta,
    ToolCallStarted,
    ToolResult,
    TurnComplete,
    TurnError,
)
from core.session import SessionStore, SessionError

from .base import BaseUI
from .panels import SkillsScreen, ToolsScreen


class MessageLine(Static):
    """一条纯文本消息。关闭 Rich markup,避免模型输出中的 [] 被误解析。"""

    def __init__(self, text: str = "", role: str = "assistant") -> None:
        super().__init__(text, markup=False)
        self._text = text
        self.add_class(role)

    def append_text(self, text: str) -> None:
        self._text += text
        self.update(self._text)


class AssistantMarkdown(Markdown):
    """助手回答的 Markdown 渲染(标题/列表/代码高亮),支持流式追加。"""

    DEFAULT_CSS = """
    AssistantMarkdown {
        width: 100%;
        margin-bottom: 1;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._source = ""

    def append_text(self, text: str) -> None:
        """流式追加片段并整体重渲染。"""
        self._source += text
        self.update(self._source)


class XAgentTUI(App, BaseUI):
    """基于 Textual 的终端界面。"""

    TITLE = "xAgent"
    SUB_TITLE = "Textual TUI"
    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("f2", "show_tools", "工具"),
        ("f3", "show_skills", "技能"),
        ("f4", "copy_last", "复制回答"),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }

    #chat-log {
        height: 1fr;
        padding: 1 2 0 2;
        background: $surface;
    }

    MessageLine {
        width: 100%;
        padding: 0 1;
        margin-bottom: 1;
    }

    .user {
        color: $text;
        border-left: solid $accent;
    }

    .assistant {
        color: $text;
    }

    .tool {
        color: $text-muted;
        background: $boost;
    }

    .error {
        color: $error;
        background: $boost;
    }

    .system {
        color: $text-muted;
        text-style: italic;
    }

    #prompt {
        margin: 0 1 1 1;
    }

    #status-bar {
        height: 1;
        margin: 0 1;
        padding: 0 1;
        color: $text-muted;
        background: $boost;
    }
    """

    def __init__(self, agent: Agent, session_store: SessionStore | None = None) -> None:
        super().__init__()
        BaseUI.__init__(self, agent)
        self._assistant_line: MessageLine | AssistantMarkdown | None = None
        self._busy = False
        # 状态栏与会话管理状态
        self._turns = 0
        self._tool_calls = 0
        self._last_answer = ""
        self._store = session_store or SessionStore()

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-log")
        yield Input(
            placeholder="输入消息,Enter 发送;F2 工具 F3 技能 F4 复制 Ctrl+Q 退出;输入 /help 查看命令",
            id="prompt",
        )
        yield Static("", id="status-bar", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._add_line(
            MessageLine(
                "xAgent TUI 已启动。"
                "F2 查看工具,F3 查看技能,F4 复制回答,Ctrl+Q 退出;"
                "输入 /help 查看会话命令。",
                "system",
            )
        )
        self._update_status()
        self.query_one("#prompt", Input).focus()

    # ===== 信息面板 =====

    def action_show_tools(self) -> None:
        """打开工具查看面板。"""
        self.push_screen(ToolsScreen(self._agent))

    def action_show_skills(self) -> None:
        """打开技能查看面板;没有可用 Skill 时给出提示。"""
        if self._agent.skills is None or len(self._agent.skills) == 0:
            self._add_line(
                MessageLine(
                    "当前没有可用的 Skill"
                    "(项目根目录下未找到 .agents/skills/ 或其中无有效 SKILL.md)。",
                    "system",
                )
            )
            return
        self.push_screen(SkillsScreen(self._agent))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        # 斜杠命令:会话管理入口,不走 LLM
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self._busy:
            self._add_line(MessageLine("正在处理上一条消息,请稍候…", "system"))
            return
        self._add_line(MessageLine(f"你: {text}", "user"))
        self._set_busy(True)
        self._run_turn(text)

    # ===== 事件渲染(均在 UI 线程执行) =====

    def _dispatch(self, event: AgentEvent) -> None:
        if isinstance(event, ContentDelta):
            if self._assistant_line is None:
                self._assistant_line = AssistantMarkdown()
                self._add_line(self._assistant_line)
            self._assistant_line.append_text(event.text)
            self._scroll_to_end()
        elif isinstance(event, ToolCallStarted):
            self._assistant_line = None
            arguments = json.dumps(event.arguments, ensure_ascii=False)
            self._add_line(MessageLine(f"[调用工具] {event.name}  参数: {arguments}", "tool"))
        elif isinstance(event, ToolResult):
            self._tool_calls += 1
            self._update_status()
            self._add_line(MessageLine(f"[工具结果] {event.result}", "tool"))
        elif isinstance(event, TurnComplete):
            self._assistant_line = None
            self._turns += 1
            self._last_answer = event.content
            self._update_status()
            self._set_busy(False)
        elif isinstance(event, TurnError):
            self._assistant_line = None
            self._add_line(MessageLine(f"[本轮出错] {event.message}", "error"))
            self._set_busy(False)

    # ===== 后台执行 =====

    @work(thread=True, exclusive=True)
    def _run_turn(self, text: str) -> None:
        """在后台线程调用 Agent,事件通过 call_from_thread 交回 UI 线程。"""
        try:
            for event in self._agent.chat(text):
                self.call_from_thread(self._dispatch, event)
        except Exception as exc:
            self.call_from_thread(self._dispatch, TurnError(str(exc)))
        finally:
            self.call_from_thread(self._set_busy, False)

    # ===== 辅助 =====

    def _add_line(self, line: MessageLine) -> None:
        self.query_one("#chat-log", VerticalScroll).mount(line)
        self.call_after_refresh(self._scroll_to_end)

    def _scroll_to_end(self) -> None:
        self.query_one("#chat-log", VerticalScroll).scroll_end(animate=False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = busy
        if not busy:
            prompt.focus()

    # ===== 会话命令 =====

    _HELP_TEXT = (
        "可用命令:\n"
        "  /help              显示本帮助\n"
        "  /clear             清空当前对话(历史随之清空)\n"
        "  /save [名称]        保存当前会话(默认按时间命名)\n"
        "  /load 名称          恢复已保存的会话\n"
        "  /sessions          列出已保存的会话\n"
        "  /export md|json [名称]  导出对话为 Markdown 或 JSON"
    )

    def _handle_command(self, text: str) -> None:
        """解析并执行 / 开头的会话命令;所有反馈以系统消息呈现。"""
        if self._busy:
            self._add_line(MessageLine("正在处理消息,命令请稍后再试。", "system"))
            return
        parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "/help":
                self._add_line(MessageLine(self._HELP_TEXT, "system"))
            elif cmd == "/clear":
                self._agent.reset()
                self.query_one("#chat-log", VerticalScroll).remove_children()
                self._assistant_line = None
                self._add_line(MessageLine("对话已清空。", "system"))
            elif cmd == "/save":
                path = self._store.save(self._agent.history, args[0] if args else None)
                self._add_line(MessageLine(f"会话已保存: {path}", "system"))
            elif cmd == "/load":
                if not args:
                    self._add_line(MessageLine("用法: /load 会话名", "system"))
                    return
                self._agent.restore_history(self._store.load(args[0]))
                self._add_line(MessageLine(f"会话「{args[0]}」已恢复,可继续对话。", "system"))
                self._render_history()
            elif cmd == "/sessions":
                items = self._store.list_sessions()
                if not items:
                    self._add_line(MessageLine("还没有已保存的会话。", "system"))
                else:
                    listing = "\n".join(f"  {name}  ({saved_at})" for name, saved_at in items)
                    self._add_line(MessageLine(f"已保存的会话:\n{listing}", "system"))
            elif cmd == "/export":
                fmt = (args[0] if args else "md").lower()
                name = args[1] if len(args) > 1 else None
                if fmt == "md":
                    path = self._store.export_markdown(self._agent.history, name)
                elif fmt == "json":
                    path = self._store.save(self._agent.history, name)
                else:
                    self._add_line(MessageLine("用法: /export md|json [名称]", "system"))
                    return
                self._add_line(MessageLine(f"已导出: {path}", "system"))
            else:
                self._add_line(MessageLine(f"未知命令 {cmd},输入 /help 查看可用命令。", "system"))
        except SessionError as exc:
            self._add_line(MessageLine(f"命令执行失败: {exc}", "error"))

    def _render_history(self) -> None:
        """把恢复的历史消息重新渲染到聊天区(纯文本概览)。"""
        for msg in self._agent.history:
            role = msg.get("role")
            if role == "user":
                self._add_line(MessageLine(f"你: {msg.get('content', '')}", "user"))
            elif role == "assistant":
                for call in msg.get("tool_calls") or []:
                    func = call.get("function", {})
                    self._add_line(MessageLine(f"[调用工具] {func.get('name')}  参数: {func.get('arguments')}", "tool"))
                if msg.get("content"):
                    self._add_line(MessageLine(str(msg["content"]), "assistant"))
            elif role == "tool":
                self._add_line(MessageLine(f"[工具结果] {msg.get('content', '')}", "tool"))

    # ===== 状态栏与复制 =====

    def _update_status(self) -> None:
        """刷新底部状态栏:模型 / 轮数 / 工具调用次数。"""
        self.query_one("#status-bar", Static).update(
            f"模型: {self._agent.model_name}   对话轮数: {self._turns}   "
            f"工具调用: {self._tool_calls} 次"
        )

    def action_copy_last(self) -> None:
        """F4:复制最近一条助手回答到系统剪贴板(OSC52)。"""
        if not self._last_answer:
            self._add_line(MessageLine("还没有可复制的回答。", "system"))
            return
        try:
            self.copy_to_clipboard(self._last_answer)
            self.notify("已复制最近一条回答", title="剪贴板")
        except Exception as exc:  # 部分终端不支持 OSC52
            self._add_line(MessageLine(f"复制失败({exc});可手动选择文本复制。", "error"))
