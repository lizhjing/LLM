"""配置加载:纯标准库实现,与具体界面无关。"""

import os
import sys
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path


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

# 首次运行自动生成的配置骨架(仅结构,不含任何密钥)
_CONFIG_TEMPLATE = """[llm]
base_url = https://api.siliconflow.cn/v1
api_key =
model = Qwen/Qwen3-8B
enable_thinking = false
timeout = 60

[ui]
typewriter_delay = 0.01
max_history = 20

[search]
api_key =
base_url = https://qianfan.baidubce.com/v2/ai_search/web_search
timeout = 30
max_results = 10
"""


def _bootstrap_user_config() -> Path | None:
    """首次运行时在用户目录生成配置文件骨架,返回其路径。"""
    try:
        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        USER_CONFIG_PATH.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
        return USER_CONFIG_PATH
    except OSError:
        return None


def _resolve_config_path(explicit: str | Path | None) -> Path:
    """按优先级确定配置文件路径。

    显式参数 > 环境变量 > 程序目录 config.ini > 用户目录 config.ini(必要时自动生成)
    """
    if explicit is not None:
        return Path(explicit)
    if os.environ.get("XAGENT_CONFIG"):
        return Path(os.environ["XAGENT_CONFIG"])
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
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

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }


@dataclass(frozen=True)
class AppConfig:
    """应用总配置。"""

    llm: LLMConfig
    ui: UIConfig
    search: SearchConfig | None = None  # 搜索为可选功能,未配置时相关工具给出提示

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppConfig":
        """从 ini 文件读取配置。

        查找顺序:显式参数 > 环境变量 XAGENT_CONFIG > 程序目录 config.ini
        > 用户目录(%APPDATA%\\xAgent\\config.ini);都没有时自动生成骨架。
        """
        cfg_path = _resolve_config_path(path)
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
        missing = [key for key in ("base_url", "api_key", "model") if not section.get(key, "").strip()]
        if missing:
            raise ValueError(f"配置文件缺少必填项: {', '.join(missing)}")

        llm = LLMConfig(
            base_url=section["base_url"].strip(),
            api_key=section["api_key"].strip(),
            model=section["model"].strip(),
            enable_thinking=cfg.getboolean("llm", "enable_thinking", fallback=False),
            timeout=cfg.getfloat("llm", "timeout", fallback=60.0),
        )
        ui = UIConfig(
            typewriter_delay=cfg.getfloat("llm", "typewriter_delay", fallback=0.01),
            max_history=cfg.getint("llm", "max_history", fallback=20),
        )

        # [search] 段与 api_key 均为可选;缺失时 searchWeb 工具向 LLM 返回未启用提示
        search = None
        if cfg.has_section("search"):
            api_key = cfg["search"].get("api_key", "").strip()
            if api_key:
                search = SearchConfig(
                    api_key=api_key,
                    base_url=cfg.get(
                        "search", "base_url", fallback="https://qianfan.baidubce.com/v2/ai_search/web_search"
                    ).strip(),
                    timeout=cfg.getfloat("search", "timeout", fallback=30.0),
                    max_results=cfg.getint("search", "max_results", fallback=10),
                )

        return cls(llm=llm, ui=ui, search=search)
