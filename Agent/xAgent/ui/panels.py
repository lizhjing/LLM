"""TUI 信息面板:Agent 技能查看与工具查看(模态界面,只读 + 受控试运行)。

与主界面保持一致的视觉规范:同样的主题变量($surface/$accent/$boost)、
圆角边框与中文文案;交互规范:表格上下选择,Enter 查看详情,Esc 返回。
面板从 Agent 只读访问器获取数据;试运行直接走 ToolRegistry.execute,
不包含任何 LLM 调用逻辑。
"""

import json

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from core.agent import Agent


class PanelScreen(ModalScreen):
    """面板基类:标题 + 数据表 + 快捷键页脚。"""

    BINDINGS = [
        ("escape", "dismiss_panel", "返回"),
        ("enter", "show_detail", "查看详情"),
    ]

    CSS = """
    PanelScreen {
        align: center middle;
    }

    #panel {
        width: 92%;
        height: 88%;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }

    #panel-title {
        width: 100%;
        padding: 1 1;
        color: $text;
        background: $boost;
        text-style: bold;
    }

    #panel-table {
        width: 100%;
        height: 1fr;
    }

    #panel-detail {
        width: 100%;
        height: 1fr;
        display: none;
        padding: 1 2;
        color: $text;
    }

    #panel-hint {
        width: 100%;
        padding: 0 1 1 1;
        color: $text-muted;
    }
    """

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self._agent = agent

    # 子类提供:面板标题 / 表格列 / 填充数据行
    def panel_title(self) -> str:
        raise NotImplementedError

    def columns(self) -> list[tuple[str, int | None]]:
        """返回 (列标题, 列宽) 列表;宽度 None 表示自适应。"""
        raise NotImplementedError

    def rows(self) -> list[tuple[str, ...]]:
        """返回数据行;每行第一列作为详情查询的主键。"""
        raise NotImplementedError

    def detail_text(self, key: str) -> str:
        """按主键生成详情文本(Enter 时调用)。"""
        raise NotImplementedError

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="panel"):
            yield Static(self.panel_title(), id="panel-title")
            table = DataTable(id="panel-table", cursor_type="row", zebra_stripes=True)
            for title, width in self.columns():
                table.add_column(title, key=title, width=width)
            yield table
            yield Static("", id="panel-detail", markup=False)
            yield Static("↑/↓ 或方向键选择 · Enter 查看详情 · Esc 返回", id="panel-hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#panel-table", DataTable)
        for row in self.rows():
            table.add_row(*row)
        if table.row_count:
            table.focus()

    def action_dismiss_panel(self) -> None:
        # 详情展开时 Esc 先收起详情;再次 Esc 才关闭面板
        detail = self.query_one("#panel-detail", Static)
        if detail.display:
            self._toggle_detail(show=False)
        else:
            self.dismiss()

    def action_show_detail(self) -> None:
        table = self.query_one("#panel-table", DataTable)
        if not table.row_count:
            return
        key = str(table.get_row_at(table.cursor_row)[0])
        detail = self.query_one("#panel-detail", Static)
        detail.update(self.detail_text(key))
        self._toggle_detail(show=True)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """DataTable 聚焦时 Enter 会触发行选中事件,与面板级 Enter 绑定等效。"""
        self.action_show_detail()

    def _toggle_detail(self, show: bool) -> None:
        """切换 列表/详情 视图,保持面板尺寸不变。"""
        table = self.query_one("#panel-table", DataTable)
        detail = self.query_one("#panel-detail", Static)
        table.display = not show
        detail.display = show
        if show:
            detail.scroll_home(animate=False)
        else:
            table.focus()


class ToolsScreen(PanelScreen):
    """Agent 工具查看:全部 Function Calling 工具的清单与参数详情,支持试运行。"""

    BINDINGS = [("r", "run_tool", "试运行")]

    def panel_title(self) -> str:
        specs = self._agent.tool_registry.specs
        return f"Agent 工具({len(specs)} 个) — 本轮对话可由模型自动调用"

    def columns(self) -> list[tuple[str, int | None]]:
        return [("工具名", 22), ("参数", 8), ("描述", None)]

    def rows(self) -> list[tuple[str, ...]]:
        rows = []
        for spec in self._agent.tool_registry.specs:
            props = spec.parameters.get("properties", {}) or {}
            required = set(spec.parameters.get("required", []) or [])
            # 必填参数标 *,直观区分可选参数
            names = ", ".join(
                name + ("*" if name in required else "") for name in props
            )
            rows.append((spec.name, names or "无", spec.description))
        return rows

    def detail_text(self, key: str) -> str:
        spec = next(s for s in self._agent.tool_registry.specs if s.name == key)
        params = json.dumps(spec.parameters, ensure_ascii=False, indent=2)
        return f"工具名: {spec.name}\n\n描述: {spec.description}\n\n参数定义(JSON Schema):\n{params}"

    def action_run_tool(self) -> None:
        """按 R:对当前选中的工具打开试运行表单。"""
        table = self.query_one("#panel-table", DataTable)
        if not table.row_count:
            return
        key = str(table.get_row_at(table.cursor_row)[0])
        self.app.push_screen(RunToolScreen(self._agent, key))


class SkillsScreen(PanelScreen):
    """Agent 技能查看:Skill 清单、正文预览与附加文件浏览(两种视图模式)。"""

    BINDINGS = [("f", "show_files", "附加文件")]

    def __init__(self, agent: Agent) -> None:
        super().__init__(agent)
        self._files_mode = False
        self._current_skill: str | None = None

    # ===== 数据行 =====

    def _skill_rows(self) -> list[tuple[str, ...]]:
        skills = self._agent.skills
        if skills is None:
            return []
        return [(meta.name, meta.description) for meta in skills.metas]

    def _file_rows(self, skill_name: str) -> list[tuple[str, ...]]:
        """列出 Skill 目录内的附加文件(SKILL.md 除外,正文随 name 加载)。"""
        skills = self._agent.skills
        assert skills is not None
        meta = next(m for m in skills.metas if m.name == skill_name)
        skill_dir = meta.path.parent
        rows = []
        for path in sorted(skill_dir.rglob("*")):
            if path.is_file() and path != meta.path:
                rows.append((path.relative_to(skill_dir).as_posix(), ""))
        return rows

    # ===== 面板结构 =====

    def panel_title(self) -> str:
        skills = self._agent.skills
        count = 0 if skills is None else len(skills)
        return f"Agent 技能({count} 个) — 模型按需调用 loadSkill 加载正文"

    def columns(self) -> list[tuple[str, int | None]]:
        return [("技能名", 24), ("描述", None)]

    def rows(self) -> list[tuple[str, ...]]:
        return self._skill_rows()

    def on_mount(self) -> None:
        self._fill_table(self._skill_rows())

    def detail_text(self, key: str) -> str:
        skills = self._agent.skills
        assert skills is not None
        if self._files_mode:
            return skills.load_file(self._current_skill or "", key)
        return skills.load_body(key)

    # ===== 模式切换 =====

    def action_show_files(self) -> None:
        """按 F:查看选中 Skill 的附加文件列表(仅在技能列表模式有效)。"""
        if self._files_mode:
            return
        table = self.query_one("#panel-table", DataTable)
        if not table.row_count:
            return
        self._current_skill = str(table.get_row_at(table.cursor_row)[0])
        self._files_mode = True
        self._fill_table(self._file_rows(self._current_skill))
        self.query_one("#panel-title", Static).update(
            f"Skill「{self._current_skill}」的附加文件 — Enter 查看内容"
        )
        self.query_one("#panel-hint", Static).update(
            "↑/↓ 选择文件 · Enter 查看内容 · Esc 返回技能列表"
        )

    def _back_to_skills(self) -> None:
        """从附加文件模式返回技能列表。"""
        self._files_mode = False
        self._current_skill = None
        self._fill_table(self._skill_rows())
        self.query_one("#panel-title", Static).update(self.panel_title())
        self.query_one("#panel-hint", Static).update(
            "↑/↓ 或方向键选择 · Enter 查看详情 · Esc 返回"
        )

    def action_dismiss_panel(self) -> None:
        """Esc 的分级返回:详情 → 技能列表 → 关闭面板。"""
        detail = self.query_one("#panel-detail", Static)
        if detail.display:
            self._toggle_detail(show=False)
        elif self._files_mode:
            self._back_to_skills()
        else:
            self.dismiss()

    def _fill_table(self, rows: list[tuple[str, ...]]) -> None:
        table = self.query_one("#panel-table", DataTable)
        table.clear()
        for row in rows:
            table.add_row(*row)
        if table.row_count:
            table.focus()


class RunToolScreen(ModalScreen):
    """工具试运行表单:按 JSON Schema 自动生成输入框,后台线程执行。"""

    BINDINGS = [("escape", "dismiss_panel", "返回")]

    CSS = """
    RunToolScreen {
        align: center middle;
    }

    #run-panel {
        width: 84%;
        max-width: 88;
        height: 90%;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }

    #run-title {
        width: 100%;
        padding: 1 1;
        background: $boost;
        text-style: bold;
    }

    #run-form {
        width: 100%;
        height: auto;
        max-height: 40%;
        padding: 0 1;
    }

    .param-label {
        width: 100%;
        color: $text-muted;
        padding-top: 1;
    }

    .param-input {
        width: 100%;
    }

    #run-btn {
        margin: 1 1 0 0;
    }

    #run-result {
        width: 100%;
        height: 1fr;
        margin-top: 1;
        padding: 1 2;
        color: $text;
        background: $boost;
        display: none;
    }
    """

    def __init__(self, agent: Agent, tool_name: str) -> None:
        super().__init__()
        self._agent = agent
        self._tool_name = tool_name

    def _spec(self):
        return next(
            s for s in self._agent.tool_registry.specs if s.name == self._tool_name
        )

    def compose(self) -> ComposeResult:
        spec = self._spec()
        props = spec.parameters.get("properties", {}) or {}
        required = set(spec.parameters.get("required", []) or [])
        with VerticalScroll(id="run-panel"):
            yield Static(f"工具试运行: {spec.name}", id="run-title")
            with VerticalScroll(id="run-form"):
                if not props:
                    yield Static("该工具无需参数。", classes="param-label")
                for name, schema in props.items():
                    star = "*" if name in required else ""
                    desc = schema.get("description", "")
                    ptype = schema.get("type", "string")
                    yield Static(f"{name}{star} ({ptype}): {desc}", classes="param-label")
                    yield Input(placeholder=f"输入 {ptype} 类型值", classes="param-input", id=f"p_{name}")
            yield Button("运行", id="run-btn", variant="primary")
            yield Static("", id="run-result", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        inputs = self.query(Input)
        if inputs:
            inputs.first().focus()
        else:
            self.query_one("#run-btn", Button).focus()

    # ===== 参数解析 =====

    def _collect_args(self) -> tuple[dict, str | None]:
        """从输入框收集并解析参数;失败时返回 (空字典, 错误信息)。"""
        spec = self._spec()
        props = spec.parameters.get("properties", {}) or {}
        required = set(spec.parameters.get("required", []) or [])
        args: dict = {}
        for name, schema in props.items():
            raw = str(self.query_one(f"#p_{name}", Input).value).strip()
            ptype = schema.get("type", "string")
            if not raw:
                if name in required:
                    return {}, f"必填参数「{name}」不能为空"
                continue  # 可选参数留空即不传
            try:
                args[name] = _parse_value(ptype, raw)
            except (ValueError, json.JSONDecodeError) as exc:
                return {}, f"参数「{name}」解析失败: {exc}"
        return args, None

    # ===== 执行 =====

    def _start_run(self) -> None:
        args, error = self._collect_args()
        result_area = self.query_one("#run-result", Static)
        if error:
            result_area.update(f"参数错误: {error}")
            result_area.display = True
            return
        self.query_one("#run-btn", Button).disabled = True
        result_area.update("正在执行…")
        result_area.display = True
        self._execute(args)

    @work(thread=True, exclusive=True)
    def _execute(self, args: dict) -> None:
        """在后台线程执行工具,避免网络/文件类工具卡住界面。"""
        result = self._agent.tool_registry.execute(self._tool_name, args)
        self.app.call_from_thread(self._show_result, args, result)

    def _show_result(self, args: dict, result: str) -> None:
        if not self.is_attached:
            return  # 面板已被 Esc 关闭,回调无处渲染,直接放弃
        args_text = json.dumps(args, ensure_ascii=False)
        self.query_one("#run-result", Static).update(
            f"参数: {args_text}\n\n执行结果:\n{result}"
        )
        self.query_one("#run-btn", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-btn":
            self._start_run()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """在任意输入框按 Enter 直接触发运行。"""
        self._start_run()

    def action_dismiss_panel(self) -> None:
        self.dismiss()


def _parse_value(ptype: str, raw: str):
    """按 JSON Schema 类型解析输入框的文本值。"""
    if ptype in ("number", "integer"):
        value = float(raw) if ptype == "number" else int(raw)
        return value
    if ptype == "boolean":
        lowered = raw.lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise ValueError("布尔值请输入 true 或 false")
    if ptype in ("array", "object"):
        return json.loads(raw)  # JSON 数组/对象,解析失败时报错给用户
    return raw


class ResumeScreen(ModalScreen):
    """历史会话选择面板:上下键选择,回车恢复,Esc 取消。"""

    BINDINGS = [("escape", "cancel_resume", "取消")]

    CSS = """
    ResumeScreen {
        align: center middle;
    }

    #resume-panel {
        width: 72%;
        max-width: 76;
        height: auto;
        max-height: 72%;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }

    #resume-title {
        width: 100%;
        padding: 1 1;
        background: $boost;
        text-style: bold;
    }

    #resume-list {
        width: 100%;
        height: auto;
        max-height: 16;
    }

    #resume-hint {
        width: 100%;
        padding: 0 1 1 1;
        color: $text-muted;
    }
    """

    def __init__(self, items: list[tuple[str, str]], on_select) -> None:
        """items 为 (会话名, 保存时间) 列表;on_select(name) 在选中时回调。"""
        super().__init__()
        self._items = items
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="resume-panel"):
            yield Static(f"历史会话({len(self._items)} 个)", id="resume-title")
            yield OptionList(id="resume-list")
            yield Static("↑/↓ 选择 · Enter 恢复 · Esc 取消", id="resume-hint")
        yield Footer()

    def on_mount(self) -> None:
        listing = self.query_one("#resume-list", OptionList)
        for name, saved_at in self._items:
            listing.add_option(Option(f"{name}  ({saved_at})", id=name))
        listing.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss()
            self._on_select(str(event.option.id))

    def action_cancel_resume(self) -> None:
        self.dismiss()


class FirstRunScreen(ModalScreen):
    """首次运行引导对话框:config.ini 缺失已自动生成 mock 配置时提示。"""

    BINDINGS = [("escape", "continue_run", "继续运行")]

    CSS = """
    FirstRunScreen {
        align: center middle;
    }

    #firstrun-panel {
        width: 82%;
        max-width: 80;
        height: auto;
        background: $surface;
        border: round $warning;
        padding: 1 2;
    }

    #firstrun-title {
        width: 100%;
        text-style: bold;
        color: $warning;
        padding-bottom: 1;
    }

    #firstrun-text {
        width: 100%;
        color: $text;
        padding-bottom: 1;
    }

    #firstrun-buttons {
        width: 100%;
        height: auto;
        align-horizontal: center;
    }

    #firstrun-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, config_path) -> None:
        super().__init__()
        self._config_path = str(config_path)

    def compose(self) -> ComposeResult:
        with Vertical(id="firstrun-panel"):
            yield Static("首次运行:已自动生成默认配置", id="firstrun-title")
            yield Static(
                "未检测到 config.ini,已在以下位置自动生成一份包含示例(mock)配置的模板:\n\n"
                f"  {self._config_path}\n\n"
                "请编辑该文件,填入你的真实 API Key 与模型等配置,然后重启 xAgent。\n"
                "在完成修改并重启之前,LLM 调用将无法正常工作。",
                id="firstrun-text",
                markup=False,
            )
            with Horizontal(id="firstrun-buttons"):
                yield Button("退出,修改后重启", id="firstrun-restart", variant="primary")
                yield Button("仍要继续运行", id="firstrun-continue", variant="default")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "firstrun-restart":
            self.app.exit()
        else:
            self.dismiss()

    def action_continue_run(self) -> None:
        self.dismiss()


class MCPScreen(ModalScreen):
    """MCP 服务器状态面板:展示已连接服务器及各自可用工具。"""

    BINDINGS = [("escape", "dismiss_panel", "返回")]

    CSS = """
    MCPScreen {
        align: center middle;
    }

    #mcp-panel {
        width: 86%;
        height: 80%;
        background: $surface;
        border: round $accent;
        padding: 0 1;
    }

    #mcp-title {
        width: 100%;
        padding: 1 1;
        background: $boost;
        text-style: bold;
    }

    #mcp-detail {
        width: 100%;
        height: 1fr;
        padding: 1 2;
        color: $text;
    }

    #mcp-hint {
        width: 100%;
        padding: 0 1 1 1;
        color: $text-muted;
    }
    """

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-panel"):
            yield Static(self._title, id="mcp-title")
            yield VerticalScroll(
                Static(self._content, id="mcp-detail", markup=False)
            )
            yield Static("Esc 返回", id="mcp-hint")
        yield Footer()

    def action_dismiss_panel(self) -> None:
        self.dismiss()
