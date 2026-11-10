"""配置加载:纯标准库实现,与具体界面无关。

API 密钥三级解析(优先级从高到低):
    1. 环境变量 XAGENT_LLM_API_KEY / XAGENT_SEARCH_API_KEY
    2. 系统凭据管理器(core.key_store,经 main.py --set-key 写入)
    3. config.ini 明文(向后兼容,不推荐)
"""

import os
import sys
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path

from .key_store import read_key

# 各密钥对应的环境变量名
_KEY_ENV_VARS = {"llm": "XAGENT_LLM_API_KEY", "search": "XAGENT_SEARCH_API_KEY"}


def _resolve_secret(env_name: str, cred_name: str, file_value: str) -> tuple[str, str]:
    """按三级优先级解析一个 API 密钥,返回 (密钥值, 来源描述)。

    来源描述用于启动时提示,取值:环境变量 / 凭据管理器 /
    config.ini 明文(提示迁移)/ 空串(未配置)。
    """
    env = os.environ.get(env_name, "").strip()
    if env:
        return env, "环境变量"
    stored = read_key(cred_name)
    if stored:
        return stored, "凭据管理器"
    if file_value:
        return file_value, "config.ini 明文"
    return "", ""


def _ui_setting(cfg: ConfigParser, key: str, default: str) -> str:
    """读取 UI 配置项:优先 [ui] 段,兼容旧版写在 [llm] 段的布局。"""
    value = cfg.get("ui", key, fallback=None)
    if value is None:
        value = cfg.get("llm", key, fallback=default)
    return value


def _detect_project_root() -> Path:
    """确定程序根目录。

    - 源码运行: core/ 的上一级(项目根)
    - PyInstaller 打包后: 可执行文件所在目录(而非 _internal 内部)
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# xAgent 项目根目录
PROJECT_ROOT = _detect_project_root()

# 用户级配置目录(打包运行时程序目录可能只读,故优先落在用户数据目录)
USER_CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "xAgent"
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.ini"

# 可用环境变量 XAGENT_CONFIG 指定配置文件位置
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("XAGENT_CONFIG") or (PROJECT_ROOT / "config.ini")
)

# 配置模板(完整结构,不含任何密钥;mock 版本由 _mock_template() 派生)
_CONFIG_TEMPLATE = """[llm]
; api_key 的三种提供方式(优先级从高到低):
;   1. 环境变量 XAGENT_LLM_API_KEY
;   2. 系统凭据管理器:python main.py --set-key llm
;   3. 直接写在本文件(明文,不推荐提交或随产物分发)
base_url = https://api.siliconflow.cn/v1
api_key =
model = Qwen/Qwen3-8B
enable_thinking = false
timeout = 120
; 建立连接的超时(秒)
connect_timeout = 10
; 模型上下文窗口大小(tokens);0 或省略 = 未知(按 128k 处理,状态栏显示「默认」)
context_window = 0

[agent]
; Agent 编排参数
max_tool_rounds = 12
compact_ratio = 0.8
chars_per_token = 3
search_results = 6
snippet_len = 240
agents_md_max_chars = 32768
agents_md_name = AGENTS.md

[tools]
result_limit = 2000
tool_timeout = 120
max_workers = 4

[mcp]
; MCP Server 连接参数(服务器清单见当前目录 .mcp.json)
connect_timeout = 30
call_timeout = 120

[ui]
typewriter_delay = 0.01
max_history = 20

[search]
; 搜索 api_key 同样支持环境变量 XAGENT_SEARCH_API_KEY 或 --set-key search
api_key =
base_url = https://qianfan.baidubce.com/v2/ai_search/web_search
timeout = 30
max_results = 10
"""

# mock 配置中的占位 API Key(非空即可通过启动校验,但调用必然失败,
# 以此引导用户修改配置后重启)
MOCK_API_KEY = "sk-MOCK-please-replace-with-your-real-key"

# 当前目录没有 config.ini 时自动生成的 mock 配置:在标准模板基础上
# 给 [llm] api_key 填入占位值(模板中第一个 "api_key =" 即 [llm] 段)
_MOCK_CONFIG_TEMPLATE = _CONFIG_TEMPLATE.replace(
    "api_key =\n", f"api_key = {MOCK_API_KEY}\n", 1
)

# 本次运行若因缺少 config.ini 而自动生成了 mock 配置,记录其路径;
# 界面层据此弹出「修改配置后重启」的引导对话框
MOCK_CREATED_PATH: Path | None = None


def _create_mock_config(path: Path) -> bool:
    """在 path 处生成含 mock 信息的完整 config.ini;失败(目录只读等)返回 False。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_MOCK_CONFIG_TEMPLATE, encoding="utf-8")
        return True
    except OSError:
        return False


def _bootstrap_user_config() -> Path | None:
    """首次运行时在用户目录生成配置文件骨架,返回其路径。"""
    try:
        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        USER_CONFIG_PATH.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        return USER_CONFIG_PATH
    except OSError:
        return None


def resolve_config_path(explicit: str | Path | None) -> Path:
    """按优先级确定配置文件路径。

    显式参数 > 环境变量 > 程序目录 config.ini(缺失时自动生成 mock 配置并记录
    到 MOCK_CREATED_PATH,由界面弹出引导)> 打包内嵌(_internal)config.ini
    > 用户目录 config.ini(必要时自动生成骨架)
    """
    if explicit is not None:
        return Path(explicit)
    if os.environ.get("XAGENT_CONFIG"):
        return Path(os.environ["XAGENT_CONFIG"])
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    # 当前目录没有 config.ini:生成含 mock 信息的完整配置并使用,
    # 界面层检测 MOCK_CREATED_PATH 弹出「修改后重启」引导
    global MOCK_CREATED_PATH
    if _create_mock_config(DEFAULT_CONFIG_PATH):
        MOCK_CREATED_PATH = DEFAULT_CONFIG_PATH
        return DEFAULT_CONFIG_PATH
    # 生成失败(目录只读等):回退原有查找链,保证程序仍可启动
    # PyInstaller 6 打包时 datas 落在 <exe目录>/_internal 下,config.ini 随包分发
    bundled = PROJECT_ROOT / "_internal" / "config.ini"
    if bundled.is_file():
        return bundled
    if USER_CONFIG_PATH.is_file():
        return USER_CONFIG_PATH
    created = _bootstrap_user_config()
    return created or DEFAULT_CONFIG_PATH


@dataclass(frozen=True)
class LLMConfig:
    """LLM 接口配置(OpenAI 兼容协议)。"""

    base_url: str
    api_key: str
    model: str
    enable_thinking: bool = False
    timeout: float = 60.0
    key_source: str = ""  # api_key 的来源(环境变量/凭据管理器/config.ini 明文),供启动提示
    # 模型上下文窗口大小(tokens);0 表示未知,运行时按 128k 默认值处理
    context_window: int = 0
    # 建立连接的超时(秒);读超时另见 timeout
    connect_timeout: float = 10.0

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }


@dataclass(frozen=True)
class UIConfig:
    """界面层配置,与 LLM 无关。"""

    typewriter_delay: float = 0.01
    max_history: int = 20


@dataclass(frozen=True)
class SearchConfig:
    """百度千帆 AI 搜索 API 配置([search] 段;缺失时搜索工具返回提示而非报错)。"""

    api_key: str
    base_url: str = "https://qianfan.baidubce.com/v2/ai_search/web_search"
    timeout: float = 30.0
    max_results: int = 10
    key_source: str = ""  # api_key 的来源,供启动提示

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }


@dataclass(frozen=True)
class AgentConfig:
    """Agent 编排层参数([agent] 段;默认值与原硬编码一致)。"""

    max_tool_rounds: int = 12       # 单轮对话的工具调用轮数上限
    compact_ratio: float = 0.8      # 上下文占用达到窗口该比例时警告并阻断输入
    chars_per_token: int = 3        # Token 估算系数(字符/Token,API 未返回 usage 时)
    search_results: int = 6         # recallContext 检索最多返回条数
    snippet_len: int = 240          # recallContext 单条命中片段最大长度
    agents_md_max_chars: int = 32768  # AGENTS.md 注入上下文的最大字符数
    agents_md_name: str = "AGENTS.md"  # 项目说明文件名(默认 AGENTS.md,可配置)


@dataclass(frozen=True)
class ToolsConfig:
    """工具执行参数([tools] 段)。"""

    result_limit: int = 2000   # 工具返回文本的统一截断上限(字符)
    tool_timeout: float = 120.0  # 工具执行超时兜底(秒)
    max_workers: int = 4       # 工具执行线程池大小


@dataclass(frozen=True)
class MCPConfig:
    """MCP Server 连接参数([mcp] 段)。"""

    connect_timeout: float = 30.0  # 连接/握手超时(秒)
    call_timeout: float = 120.0    # 工具调用超时(秒)


@dataclass(frozen=True)
class AppConfig:
    """应用总配置。"""

    llm: LLMConfig
    ui: UIConfig
    search: SearchConfig | None = None  # 搜索为可选功能,未配置时相关工具给出提示
    agent: AgentConfig = field(default_factory=AgentConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppConfig":
        """从 ini 文件读取配置。

        查找顺序:显式参数 > 环境变量 XAGENT_CONFIG > 程序目录 config.ini
        > 用户目录(%APPDATA%\\xAgent\\config.ini);都没有时自动生成骨架。
        """
        cfg_path = resolve_config_path(path)
        if not cfg_path.is_file():
            raise FileNotFoundError(
                f"配置文件不存在: {cfg_path}\n"
                f"请把 config.ini 放到 {cfg_path.parent} 目录下,"
                f"或用 --config 参数 / 环境变量 XAGENT_CONFIG 指定路径。"
            )

        cfg = ConfigParser()
        cfg.read(cfg_path, encoding="utf-8")
        if "llm" not in cfg:
            raise ValueError(f"配置文件缺少 [llm] 段: {cfg_path}")

        section = cfg["llm"]
        llm_key, llm_key_source = _resolve_secret(
            _KEY_ENV_VARS["llm"], "llm", section.get("api_key", "").strip()
        )
        missing = [key for key in ("base_url", "model") if not section.get(key, "").strip()]
        if not llm_key:
            missing.append("api_key")
        if missing:
            hint = (
                ";api_key 可通过环境变量 XAGENT_LLM_API_KEY 提供,"
                "或运行 python main.py --set-key llm 存入系统凭据管理器"
                if "api_key" in missing
                else ""
            )
            raise ValueError(f"配置文件缺少必填项: {', '.join(missing)}" + hint)

        llm = LLMConfig(
            base_url=section["base_url"].strip(),
            api_key=llm_key,
            model=section["model"].strip(),
            enable_thinking=cfg.getboolean("llm", "enable_thinking", fallback=False),
            timeout=cfg.getfloat("llm", "timeout", fallback=60.0),
            key_source=llm_key_source,
            context_window=cfg.getint("llm", "context_window", fallback=0),
            connect_timeout=cfg.getfloat("llm", "connect_timeout", fallback=10.0),
        )
        ui = UIConfig(
            typewriter_delay=float(_ui_setting(cfg, "typewriter_delay", "0.01")),
            max_history=int(_ui_setting(cfg, "max_history", "20")),
        )
        # 三个可选段:缺失时直接用默认值(与原硬编码一致),老配置文件无需改动
        agent = AgentConfig(
            max_tool_rounds=cfg.getint("agent", "max_tool_rounds", fallback=12),
            compact_ratio=cfg.getfloat("agent", "compact_ratio", fallback=0.8),
            chars_per_token=cfg.getint("agent", "chars_per_token", fallback=3),
            search_results=cfg.getint("agent", "search_results", fallback=6),
            snippet_len=cfg.getint("agent", "snippet_len", fallback=240),
            agents_md_max_chars=cfg.getint("agent", "agents_md_max_chars", fallback=32768),
            agents_md_name=cfg.get("agent", "agents_md_name", fallback="AGENTS.md"),
        )
        tools = ToolsConfig(
            result_limit=cfg.getint("tools", "result_limit", fallback=2000),
            tool_timeout=cfg.getfloat("tools", "tool_timeout", fallback=120.0),
            max_workers=cfg.getint("tools", "max_workers", fallback=4),
        )
        mcp = MCPConfig(
            connect_timeout=cfg.getfloat("mcp", "connect_timeout", fallback=30.0),
            call_timeout=cfg.getfloat("mcp", "call_timeout", fallback=120.0),
        )

        # [search] 段为可选;api_key 为空时还会尝试环境变量/凭据管理器,
        # 三级来源都取不到则 search 为 None,searchWeb 工具向 LLM 返回未启用提示
        search = None
        if cfg.has_section("search"):
            search_key, search_key_source = _resolve_secret(
                _KEY_ENV_VARS["search"], "search", cfg["search"].get("api_key", "").strip()
            )
            if search_key:
                search = SearchConfig(
                    api_key=search_key,
                    base_url=cfg.get(
                        "search", "base_url", fallback="https://qianfan.baidubce.com/v2/ai_search/web_search"
                    ).strip(),
                    timeout=cfg.getfloat("search", "timeout", fallback=30.0),
                    max_results=cfg.getint("search", "max_results", fallback=10),
                    key_source=search_key_source,
                )

        return cls(llm=llm, ui=ui, search=search, agent=agent, tools=tools, mcp=mcp)
