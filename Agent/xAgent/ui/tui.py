"""Textual TUI 界面:只负责渲染 Agent 事件,不包含任何 LLM/工具逻辑。"""

import json
import time
from dataclasses import dataclass
from typing import Callable

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from core.agent import Agent
from core import config as core_config
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
from core.session import SessionStore, SessionError

from .base import BaseUI
from .panels import FirstRunScreen, MCPScreen, ResumeScreen, SkillsScreen, ToolsScreen


@dataclass(frozen=True)
class SlashCommand:
    """一条斜杠命令的定义:name 不含斜杠;args_hint 为空表示无参数。"""

    name: str
    help: str
    args_hint: str
    handler: Callable[[list[str]], None]


class CommandInput(Input):
    """输入框:命令补全面板可见时拦截导航键,交给宿主 App 处理。

    补全面板本身由 App 组建与管理;这里只做按键分流,
    其余按键仍走 Input 默认处理(输入/提交/历史等)。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._completion_panel: OptionList | None = None  # 由 App 在 on_mount 注入

    async def _on_key(self, event: events.Key) -> None:
        panel = self._completion_panel
        if panel is not None and panel.has_class("visible"):
            if event.key == "down":
                panel.action_cursor_down()
            elif event.key == "up":
                panel.action_cursor_up()
            elif event.key in ("tab", "enter"):
                self.app.accept_completion()
            elif event.key == "escape":
                self.app.hide_completions()
            else:
                await super()._on_key(event)
                return
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


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

    # 关闭鼠标文本选择:Textual 8.2.8 在 Markdown 子块(如 --- 分隔线)上按下鼠标时,
    # selection 逻辑取 parent 为 None 直接崩溃(screen.py _forward_event);
    # 设为 False 后 MouseDown 走非 selection 分支,彻底绕开该缺陷。
    # 复制回答仍可用 F4。
    allow_select = False

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
        self._last_flush = 0.0

    # 刷新最小间隔(秒):流式期间按时间节流批量重渲染,避免逐 token 全量刷新
    _FLUSH_INTERVAL = 0.05

    def append_text(self, text: str) -> None:
        """流式追加片段;节流刷新,长回答不再 O(n²) 卡顿。"""
        self._source += text
        now = time.monotonic()
        if now - self._last_flush >= self._FLUSH_INTERVAL:
            self._last_flush = now
            self.update(self._source)

    def flush(self) -> None:
        """强制渲染剩余片段(轮次结束/切换前调用,保证尾部不丢字)。"""
        if self._source:
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

    #body {
        height: 1fr;
    }

    #chat-log {
        width: 1fr;
        height: 1fr;
        padding: 1 2 0 2;
        background: $surface;
    }

    #sidebar {
        width: 32;
        min-width: 20;
        border-left: solid $boost;
        padding: 0 1;
        background: $surface;
    }

    #sidebar-title {
        width: 100%;
        color: $text-muted;
        text-style: bold;
        padding-bottom: 1;
    }

    #sidebar-scroll {
        height: 1fr;
    }

    #sidebar-mcp {
        width: 100%;
        color: $text;
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

    #cmd-complete {
        display: none;
        max-height: 8;
        margin: 0 1;
        background: $surface;
        border: round $accent;
    }

    #cmd-complete.visible {
        display: block;
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

    def __init__(
        self,
        agent: Agent,
        session_store: SessionStore | None = None,
        mcp_loader: Callable[[], object] | None = None,
        mcp_conns_sink: list | None = None,
    ) -> None:
        super().__init__()
        BaseUI.__init__(self, agent)
        self._assistant_line: MessageLine | AssistantMarkdown | None = None
        self._busy = False
        # 状态栏与会话管理状态
        self._turns = 0
        self._tool_calls = 0
        self._last_answer = ""
        self._store = session_store or SessionStore()
        # MCP 异步加载:loader 由 main 注入(返回 MCPLoadResult 形状的对象);
        # 连接列表写入 sink,由 main 在退出时统一关停
        self._mcp_loader = mcp_loader
        self._mcp_conns_sink = mcp_conns_sink
        self._mcp_servers: list = []  # MCPServerInfo 形状的对象(鸭子类型,不导入 tools 层)
        self._mcp_loaded = False
        # 斜杠命令注册表(on_mount 构建);补全期间程序化改输入框时抑制 Changed 重弹
        self._commands: dict[str, SlashCommand] = {}
        self._completion_lock = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield VerticalScroll(id="chat-log")
            with Vertical(id="sidebar"):
                yield Static("MCP 服务器", id="sidebar-title")
                yield VerticalScroll(
                    Static("正在初始化…", id="sidebar-mcp", markup=False),
                    id="sidebar-scroll",
                )
        yield OptionList(id="cmd-complete")
        yield CommandInput(
            placeholder="输入消息,Enter 发送;输入 / 唤起命令补全;/help 查看命令",
            id="prompt",
        )
        yield Static("", id="status-bar", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._add_line(
            MessageLine(
                "xAgent TUI 已启动。"
                "F2 工具,F3 技能,F4 复制回答,Ctrl+Q 退出;"
                "输入 / 唤起命令补全,/help 查看全部命令。",
                "system",
            )
        )
        self._commands = self._build_commands()
        # 输入框认识补全面板,便于按键拦截分流
        self.query_one("#prompt", CommandInput)._completion_panel = self.query_one(
            "#cmd-complete", OptionList
        )
        self._update_status()
        self._update_mcp_sidebar()
        self.query_one("#prompt", Input).focus()
        # config.ini 缺失时本次启动自动生成了 mock 配置:弹出引导对话框
        # (动态读模块属性:main 中 resolve 先于 TUI 导入,但取最新值更稳)
        if core_config.MOCK_CREATED_PATH is not None:
            self.push_screen(FirstRunScreen(core_config.MOCK_CREATED_PATH))
        # MCP 异步加载:先进入界面,连接完成后再动态刷新侧栏与工具注册表
        if self._mcp_loader is not None:
            self._load_mcp_async()
        else:
            self._mcp_loaded = True
            self._update_mcp_sidebar()

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

    def on_input_changed(self, event: Input.Changed) -> None:
        """输入变化时刷新命令补全面板(仅主输入框;程序化赋值时抑制)。"""
        if event.input.id != "prompt":
            return
        if self._completion_lock:
            self._completion_lock = False
            return
        self._update_completions(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.hide_completions()
        text = event.input.value.strip()
        if not text:
            return
        # 斜杠命令:会话管理入口,不走 LLM
        if text.startswith("/"):
            event.input.value = ""
            self._handle_command(text)
            return
        if self._busy:
            self._add_line(MessageLine("正在处理上一条消息,请稍候…", "system"))
            return
        # 上下文达到阈值:暂停普通提示词,引导用户压缩(输入保留,压缩后可直接重发)
        if self._agent.context_blocked:
            self._add_line(
                MessageLine(
                    "上下文占用已达阈值,已暂停处理新消息;请输入 /compact 压缩上下文后继续。",
                    "system",
                )
            )
            return
        event.input.value = ""  # busy 检查通过后才清空,用户输入不再被吞
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
            if self._assistant_line is not None:
                self._assistant_line.flush()  # 渲染节流的尾部字符
            self._assistant_line = None
            arguments = json.dumps(event.arguments, ensure_ascii=False)
            self._add_line(MessageLine(f"[调用工具] {event.name}  参数: {arguments}", "tool"))
        elif isinstance(event, ToolResult):
            self._tool_calls += 1
            self._update_status()
            self._add_line(MessageLine(f"[工具结果] {event.result}", "tool"))
        elif isinstance(event, TurnComplete):
            if self._assistant_line is not None:
                self._assistant_line.flush()
            self._assistant_line = None
            self._turns += 1
            self._last_answer = event.content
            self._update_status()
            self._set_busy(False)
        elif isinstance(event, TurnError):
            if self._assistant_line is not None:
                self._assistant_line.flush()
            self._assistant_line = None
            self._add_line(MessageLine(f"[本轮出错] {event.message}", "error"))
            self._set_busy(False)
        elif isinstance(event, ContextWarning):
            # 本轮已正常完成(TurnComplete 先到),此处只做醒目提示
            if self._assistant_line is not None:
                self._assistant_line.flush()
            self._assistant_line = None
            self._add_line(MessageLine(f"[上下文告警] {event.message}", "error"))
        elif isinstance(event, CompactionComplete):
            # 压缩期间摘要以流式增量渲染;到这步历史已被替换,状态栏随之刷新
            if self._assistant_line is not None:
                self._assistant_line.flush()
            self._assistant_line = None
            self._update_status()
            self._add_line(
                MessageLine(
                    f"上下文压缩完成。原始上下文已归档(编号 {event.archive_id}),"
                    "历史已替换为摘要,可继续对话;缺少历史细节时 Agent 可用 recallContext 检索。",
                    "system",
                )
            )
            self._set_busy(False)

    # ===== 后台执行 =====

    @work(thread=True, exclusive=True)
    def _run_turn(self, text: str) -> None:
        """在后台线程调用 Agent,事件通过 call_from_thread 交回 UI 线程。"""
        try:
            for event in self._agent.chat(text):
                self._post(event)
        except Exception as exc:
            self._post(TurnError(str(exc)))
        finally:
            self._post(None)

    @work(thread=True, exclusive=True)
    def _run_compaction(self) -> None:
        """在后台线程执行上下文压缩(/compact),事件处理与对话轮一致。"""
        try:
            for event in self._agent.compact():
                self._post(event)
        except Exception as exc:
            self._post(TurnError(str(exc)))
        finally:
            self._post(None)

    def _post(self, event: AgentEvent | None) -> None:
        """向 UI 线程投递事件;应用退出中界面不可达时静默放弃。

        event 为 None 表示仅解除 busy 状态。
        """
        try:
            if event is None:
                self.call_from_thread(self._set_busy, False)
            else:
                self.call_from_thread(self._dispatch, event)
        except RuntimeError:
            pass  # 退出竞态:Textual 已停止消息泵,无法再投递

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
        # 仅主屏时回收焦点:F2/F3 面板打开时不抢占用户焦点
        if not busy and len(self.screen_stack) == 1:
            prompt.focus()

    # ===== 会话命令(斜杠命令注册表) =====

    def _build_commands(self) -> dict[str, SlashCommand]:
        """斜杠命令注册表:/help 文本与补全面板均由此自动生成。"""
        return {
            "help": SlashCommand("help", "显示本帮助", "", lambda args: self._cmd_help()),
            "new": SlashCommand("new", "自动保存当前会话后新建会话", "", self._cmd_new),
            "resume": SlashCommand("resume", "打开历史会话列表面板恢复会话", "", self._cmd_resume),
            "save": SlashCommand("save", "保存当前会话", "[名称]", self._cmd_save),
            "load": SlashCommand("load", "恢复指定会话(免面板)", "名称", self._cmd_load),
            "sessions": SlashCommand("sessions", "列出已保存的会话", "", self._cmd_sessions),
            "export": SlashCommand("export", "导出对话为 Markdown 或 JSON", "md|json [名称]", self._cmd_export),
            "clear": SlashCommand("clear", "清空当前对话(不保存)", "", self._cmd_clear),
            "compact": SlashCommand("compact", "压缩上下文(归档原始记录,以摘要替换历史)", "", self._cmd_compact),
            "tools": SlashCommand("tools", "查看全部工具面板(同 F2)", "", self._cmd_tools),
            "skills": SlashCommand("skills", "查看技能面板(同 F3)", "", self._cmd_skills),
            "mcp": SlashCommand("mcp", "查看 MCP 服务器与工具详情", "", self._cmd_mcp),
            "quit": SlashCommand("quit", "退出 xAgent", "", self._cmd_quit),
        }

    def _handle_command(self, text: str) -> None:
        """解析并执行 / 开头的命令;所有反馈以系统消息呈现。"""
        if self._busy:
            self._add_line(MessageLine("正在处理消息,命令请稍后再试。", "system"))
            return
        parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]
        command = self._commands.get(cmd[1:])
        if command is None:
            self._add_line(MessageLine(f"未知命令 {cmd},输入 /help 查看可用命令。", "system"))
            return
        try:
            command.handler(args)
        except (SessionError, OSError) as exc:
            self._add_line(MessageLine(f"命令执行失败: {exc}", "error"))

    def _cmd_help(self) -> None:
        lines = ["可用命令(输入 / 可唤起补全面板):"]
        for command in self._commands.values():
            signature = f"/{command.name} {command.args_hint}".rstrip()
            lines.append(f"  {signature:<20} {command.help}")
        self._add_line(MessageLine("\n".join(lines), "system"))

    def _cmd_new(self, args: list[str]) -> None:
        """新建会话:先自动保存当前会话历史,再清空历史与界面。"""
        try:
            path = self._store.save(self._agent.history)
            note = f"当前会话已自动保存: {path}\n"
        except (SessionError, OSError) as exc:
            note = f"自动保存失败({exc}),仍将新建会话。\n"
        self._agent.reset()
        self._clear_chat()
        self._turns = 0
        self._tool_calls = 0
        self._last_answer = ""
        self._update_status()
        self._add_line(MessageLine(note + "已新建会话。", "system"))

    def _cmd_resume(self, args: list[str]) -> None:
        """打开历史会话列表面板:上下键选择,回车恢复,Esc 取消。"""
        items = self._store.list_sessions()
        if not items:
            self._add_line(MessageLine("还没有已保存的会话。", "system"))
            return
        self.push_screen(ResumeScreen(items, self._restore_session))

    def _restore_session(self, name: str) -> None:
        """恢复指定会话:替换历史并重绘聊天区。"""
        try:
            messages = self._store.load(name)
        except (SessionError, OSError) as exc:
            self._add_line(MessageLine(f"会话恢复失败: {exc}", "error"))
            return
        self._agent.restore_history(messages)
        self._clear_chat()
        self._turns = 0
        self._tool_calls = 0
        self._last_answer = ""
        self._add_line(MessageLine(f"会话「{name}」已恢复,可继续对话。", "system"))
        self._render_history()
        self._update_status()

    def _cmd_save(self, args: list[str]) -> None:
        path = self._store.save(self._agent.history, args[0] if args else None)
        self._add_line(MessageLine(f"会话已保存: {path}", "system"))

    def _cmd_load(self, args: list[str]) -> None:
        if not args:
            self._add_line(MessageLine("用法: /load 会话名(或用 /resume 打开选择面板)", "system"))
            return
        self._restore_session(args[0])

    def _cmd_sessions(self, args: list[str]) -> None:
        items = self._store.list_sessions()
        if not items:
            self._add_line(MessageLine("还没有已保存的会话。", "system"))
        else:
            listing = "\n".join(f"  {name}  ({saved_at})" for name, saved_at in items)
            self._add_line(MessageLine(f"已保存的会话:\n{listing}", "system"))

    def _cmd_export(self, args: list[str]) -> None:
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

    def _cmd_clear(self, args: list[str]) -> None:
        self._agent.reset()
        self._clear_chat()
        self._turns = 0
        self._tool_calls = 0
        self._last_answer = ""
        self._update_status()
        self._add_line(MessageLine("对话已清空。", "system"))

    def _cmd_compact(self, args: list[str]) -> None:
        self._add_line(
            MessageLine(
                "开始压缩上下文:先归档原始记录到 .agents/context/,"
                "再由 LLM 生成摘要替换历史;期间暂停处理新消息…",
                "system",
            )
        )
        self._set_busy(True)
        self._run_compaction()

    def _cmd_tools(self, args: list[str]) -> None:
        self.action_show_tools()

    def _cmd_skills(self, args: list[str]) -> None:
        self.action_show_skills()

    def _cmd_mcp(self, args: list[str]) -> None:
        self.push_screen(MCPScreen("MCP 服务器", self._mcp_panel_content()))

    def _cmd_quit(self, args: list[str]) -> None:
        self.exit()

    def _clear_chat(self) -> None:
        """清空聊天区并丢弃未完成的流式渲染行。"""
        self._assistant_line = None
        self.query_one("#chat-log", VerticalScroll).remove_children()

    # ===== 斜杠命令补全面板 =====

    def _update_completions(self, value: str) -> None:
        """按当前输入前缀过滤命令,弹出/刷新补全面板。"""
        panel = self.query_one("#cmd-complete", OptionList)
        if not value.startswith("/") or " " in value:
            self.hide_completions()
            return
        prefix = value[1:].lower()
        matches = [c for c in self._commands.values() if c.name.startswith(prefix)]
        if not matches:
            self.hide_completions()
            return
        panel.clear_options()
        for command in matches:
            signature = f"/{command.name} {command.args_hint}".rstrip()
            panel.add_option(
                Option(f"{signature}  —  {command.help}", id=command.name)
            )
        if panel.highlighted is None:
            panel.highlighted = 0
        panel.add_class("visible")

    def hide_completions(self) -> None:
        self.query_one("#cmd-complete", OptionList).remove_class("visible")

    def accept_completion(self) -> None:
        """把当前高亮的补全项填入输入框;带参数的命令自动补一个空格。"""
        panel = self.query_one("#cmd-complete", OptionList)
        index = panel.highlighted
        if index is None or panel.option_count == 0:
            self.hide_completions()
            return
        command = self._commands[str(panel.get_option_at_index(index).id)]
        value = f"/{command.name}" + (" " if command.args_hint else "")
        prompt = self.query_one("#prompt", Input)
        self._completion_lock = True  # 程序化赋值,不让 Changed 重新弹面板
        prompt.value = value
        prompt.cursor_position = len(value)
        self.hide_completions()
        prompt.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """鼠标点击补全项时同样视为接受(会话面板有自己的同名处理器)。"""
        if event.option_list.id == "cmd-complete":
            event.stop()
            self.accept_completion()

    # ===== MCP 边栏与异步加载 =====

    @work(thread=True, exclusive=True)
    def _load_mcp_async(self) -> None:
        """后台线程连接 MCP Server:不阻塞界面,完成后交回 UI 线程刷新。"""
        try:
            result = self._mcp_loader()
            error = None
        except Exception as exc:  # 加载器抛出的任何异常都以错误状态呈现
            result, error = None, str(exc)
        self.call_from_thread(self._on_mcp_loaded, result, error)

    def _on_mcp_loaded(self, result: object | None, error: str | None) -> None:
        """MCP 加载完成(UI 线程):注册工具、记录连接、刷新侧栏与状态栏。

        对 result 采用鸭子类型访问(不导入 tools 层),要求其具备
        notes/specs/connections/servers 四个属性。
        """
        self._mcp_loaded = True
        if error is not None:
            self._add_line(MessageLine(f"[MCP] 加载失败: {error}", "error"))
            self._update_mcp_sidebar()
            self._update_status()
            return
        for note in getattr(result, "notes", []):
            self._add_line(MessageLine(str(note), "system"))
        self._mcp_servers = list(getattr(result, "servers", []))
        if self._mcp_conns_sink is not None:
            self._mcp_conns_sink.extend(getattr(result, "connections", []))
        registered = 0
        for spec in getattr(result, "specs", []):
            try:
                self._agent.tool_registry.register(spec)
                registered += 1
            except ValueError as exc:
                self._add_line(MessageLine(f"[MCP 警告] {exc}", "error"))
        connected = [s for s in self._mcp_servers if getattr(s, "connected", False)]
        if connected:
            self._add_line(
                MessageLine(
                    f"MCP 加载完成:{len(connected)} 个服务器,新增 {registered} 个工具;输入 /mcp 查看详情。",
                    "system",
                )
            )
        self._update_status()
        self._update_mcp_sidebar()

    def _update_mcp_sidebar(self) -> None:
        """刷新侧栏 MCP 区域(加载中/未配置/已加载三种状态)。"""
        area = self.query_one("#sidebar-mcp", Static)
        if not self._mcp_loaded:
            area.update("正在连接 MCP 服务器…")
            return
        if not self._mcp_servers:
            area.update(
                "未检测到 .mcp.json\n\n"
                "未启用 MCP 服务器。\n"
                "可在当前目录创建\n"
                ".mcp.json 后重启。"
            )
            return
        lines: list[str] = []
        for info in self._mcp_servers:
            if getattr(info, "connected", False):
                tools = list(getattr(info, "tools", []))
                lines.append(f"● {info.name}({len(tools)} 工具)")
                lines.extend(f"  · {name}" for name in tools)
            else:
                lines.append(f"○ {info.name}(连接失败)")
                error = " ".join(str(getattr(info, "error", "")).split())
                lines.append(f"  {error[:64]}")
            lines.append("")
        area.update("\n".join(lines).rstrip())

    def _mcp_panel_content(self) -> str:
        """/mcp 面板的正文:与侧栏同源,展示更完整的信息。"""
        if not self._mcp_loaded:
            return "正在连接 MCP 服务器,请稍后再试。"
        if not self._mcp_servers:
            return (
                "未检测到 .mcp.json,当前未连接任何 MCP 服务器。\n"
                '在当前目录创建 .mcp.json(格式:{"mcpServers": {...}})并重启即可启用。'
            )
        lines: list[str] = []
        for info in self._mcp_servers:
            if getattr(info, "connected", False):
                tools = list(getattr(info, "tools", []))
                lines.append(f"● {info.name}  已连接,共 {len(tools)} 个工具")
                lines.extend(f"   - {name}" for name in tools)
            else:
                lines.append(f"○ {info.name}  连接失败")
                lines.append(f"   {getattr(info, 'error', '')}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _render_history(self) -> None:
        """把恢复的历史消息重新渲染到聊天区(纯文本概览)。"""
        for msg in self._agent.history:
            role = msg.get("role")
            if role == "user":
                self._add_line(MessageLine(f"你: {msg.get('content', '')}", "user"))
            elif role == "assistant":
                for call in msg.get("tool_calls") or []:
                    func = call.get("function", {})
                    arguments = json.dumps(func.get("arguments"), ensure_ascii=False)
                    self._add_line(MessageLine(f"[调用工具] {func.get('name')}  参数: {arguments}", "tool"))
                if msg.get("content"):
                    self._add_line(MessageLine(str(msg["content"]), "assistant"))
            elif role == "tool":
                self._add_line(MessageLine(f"[工具结果] {msg.get('content', '')}", "tool"))

    # ===== 状态栏与复制 =====

    def _update_status(self) -> None:
        """刷新底部状态栏:模型 / 上下文占用 / MCP / 轮数 / 工具调用次数。"""
        self.query_one("#status-bar", Static).update(
            f"模型: {self._agent.model_name}   {self._context_status_text()}   "
            f"{self._mcp_status_text()}   对话轮数: {self._turns}   工具调用: {self._tool_calls} 次"
        )

    def _mcp_status_text(self) -> str:
        """状态栏中的 MCP 摘要:加载中 / 未连接 / 服务器与工具数。"""
        if not self._mcp_loaded:
            return "MCP: 加载中"
        connected = [s for s in self._mcp_servers if getattr(s, "connected", False)]
        if not connected:
            return "MCP: 未连接"
        tools = sum(len(getattr(s, "tools", [])) for s in connected)
        return f"MCP: {len(connected)} 服务器/{tools} 工具"

    def _context_status_text(self) -> str:
        """上下文占用展示:已用 k / 窗口 k(百分比)。

        窗口大小未知时按默认 128k 计算并标注「默认」;达到阈值加 ⚠ 醒目提示。
        """
        used = self._agent.context_tokens
        window = self._agent.context_window
        percent = used / window * 100 if window > 0 else 0.0
        mark = "「默认」" if self._agent.context_window_is_default else ""
        warn = "⚠" if percent >= 80 else ""
        return (
            f"上下文{warn}: {used / 1024:.1f}k/{window / 1024:.0f}k{mark}"
            f"({percent:.0f}%)"
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
