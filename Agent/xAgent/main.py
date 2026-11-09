"""xAgent 启动入口:装配配置、工具、Agent 与界面。

运行方式:
    python main.py                # 默认 Textual TUI
    python main.py --ui console   # 传统命令行界面
    python main.py --set-key llm    # 录入 LLM API key 并存入系统凭据管理器
    python main.py --clear-key llm  # 从系统凭据管理器删除 LLM API key
"""

import argparse
from pathlib import Path

from core.agent import Agent
from core.config import AppConfig, MOCK_CREATED_PATH, resolve_config_path
from core.logging_setup import setup_logging
from core.skills import SkillRegistry
from tools import build_default_registry
from tools.context_tools import build_context_tools
from tools.mcp_tools import build_mcp_tools
from ui import available_uis, create_ui


def _manage_key(name: str, setting: bool) -> None:
    """命令行密钥管理:向系统凭据管理器写入或删除 API key。

    密钥经 DPAPI 加密并与当前用户绑定,替代 config.ini 明文存储。
    """
    from core import key_store

    if not setting:
        ok = key_store.delete_key(name)
        print(f"{'已删除' if ok else '未找到'}凭据条目: {key_store.credential_target(name)}")
        return

    import getpass

    label = "SiliconFlow(LLM)" if name == "llm" else "百度千帆(搜索)"
    key = getpass.getpass(f"请输入 {label} API key(输入不回显): ").strip()
    if not key:
        print("已取消:未输入任何内容")
        return
    if key_store.save_key(name, key):
        print(f"已保存到系统凭据管理器: {key_store.credential_target(name)}")
        print("config.ini 中的 api_key 可留空,明文密钥建议删除。")
    else:
        print("保存失败:当前环境不支持系统凭据管理器,"
              "可改用环境变量 XAGENT_LLM_API_KEY / XAGENT_SEARCH_API_KEY。")


def main() -> None:
    log_path = setup_logging() / "xagent.log"  # 诊断日志:本地日志目录,不打扰终端
    parser = argparse.ArgumentParser(description="xAgent:core/UI 分层的本地 Agent")
    parser.add_argument(
        "--ui",
        default="tui",
        choices=available_uis(),
        help="界面类型(默认 tui)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径(默认 xAgent/config.ini)",
    )
    parser.add_argument(
        "--set-key",
        choices=("llm", "search"),
        metavar="{llm,search}",
        help="交互式录入 API key 并存入系统凭据管理器(推荐,替代 config.ini 明文)",
    )
    parser.add_argument(
        "--clear-key",
        choices=("llm", "search"),
        metavar="{llm,search}",
        help="从系统凭据管理器删除已保存的 API key",
    )
    args = parser.parse_args()

    if args.set_key or args.clear_key:
        _manage_key(args.set_key or args.clear_key, setting=bool(args.set_key))
        return

    # 显式打印实际加载的配置文件与模型,避免"改了另一份 config.ini 未生效"的排查成本
    cfg_path = resolve_config_path(args.config)
    print(f"[配置] {cfg_path}")
    print(f"[日志] {log_path}")
    # 当前目录原本没有 config.ini:已自动生成含 mock 信息的完整配置,
    # 除终端提示外,TUI 还会弹出引导对话框(修改并重启后才会正常工作)
    if MOCK_CREATED_PATH is not None:
        print("[引导] 未检测到 config.ini,已自动生成含示例(mock)配置的模板;")
        print(f"       请编辑 {MOCK_CREATED_PATH} 填入真实 API Key,然后重启 xAgent。")
    config = AppConfig.load(cfg_path)
    print(f"[模型] {config.llm.model}  @ {config.llm.base_url}")
    search_note = f"search: {config.search.key_source}" if config.search else "search: 未启用"
    print(f"[密钥] llm: {config.llm.key_source or '未配置'};{search_note}")
    if config.llm.key_source == "config.ini 明文":
        print("[提示] API key 以明文存于 config.ini,建议运行 python main.py --set-key llm "
              "迁入系统凭据管理器后删除明文。")

    # Skill 发现:同时扫描启动工作目录与程序目录的 .agents/skills/,
    # 内置技能与启动位置无关始终可用;启动目录的同名 Skill 优先(可覆盖内置)
    program_root = Path(__file__).resolve().parent
    skills = SkillRegistry.scan(
        Path.cwd() / ".agents" / "skills",
        program_root / ".agents" / "skills",
    )
    for problem in skills.problems:
        print(f"[Skill 警告] {problem}")

    # 内置工具:参数来自 [tools] 配置段;上下文检索工具配合 /compact 使用,
    # 归档存放在启动工作目录的 .agents/context/,检索参数来自 [agent]
    registry = build_default_registry(config.search, skills, config.tools)
    context_dir = Path.cwd() / ".agents" / "context"
    for spec in build_context_tools(context_dir):
        registry.register(spec)

    try:
        agent = Agent(config, registry, skills=skills, context_dir=context_dir)
        if args.ui == "tui":
            # TUI:先进入界面,MCP 在后台线程异步连接,完成后动态刷新侧栏;
            # 连接对象写入 sink,由下方 finally 在退出时统一关停
            mcp_conns: list = []
            create_ui(
                args.ui,
                agent,
                config,
                mcp_loader=lambda: build_mcp_tools(
                    Path.cwd(), config.mcp.connect_timeout, config.mcp.call_timeout
                ),
                mcp_conns_sink=mcp_conns,
            ).run()
            conns_to_stop = mcp_conns
        else:
            # Console:MCP 同步加载完成后进入界面(无界面刷新诉求)
            result = build_mcp_tools(
                Path.cwd(), config.mcp.connect_timeout, config.mcp.call_timeout
            )
            for note in result.notes:
                print(note)
            for spec in result.specs:
                try:
                    registry.register(spec)
                except ValueError as exc:
                    print(f"[MCP 警告] {exc}")
            create_ui(args.ui, agent, config).run()
            conns_to_stop = result.connections
    finally:
        # 显式关停 MCP 连接(atexit 仅作兜底),退出可控不卡顿
        for conn in locals().get("conns_to_stop", []):
            try:
                conn.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
