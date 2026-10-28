"""xAgent 启动入口:装配配置、工具、Agent 与界面。

运行方式:
    python main.py                # 默认 Textual TUI
    python main.py --ui console   # 传统命令行界面
"""

import argparse
from pathlib import Path

from core.agent import Agent
from core.config import AppConfig
from core.skills import SkillRegistry
from tools import build_default_registry
from ui import available_uis, create_ui


def main() -> None:
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
    args = parser.parse_args()

    config = AppConfig.load(args.config)

    # Skill 发现:以程序启动时的工作目录为项目根目录,扫描 .agents/skills/
    # 下的各 Skill 子目录(仅加载 name 与 description 元信息)
    skills = SkillRegistry.scan(Path.cwd() / ".agents" / "skills")
    for problem in skills.problems:
        print(f"[Skill 警告] {problem}")

    agent = Agent(config, build_default_registry(config.search, skills), skills=skills)
    create_ui(args.ui, agent, config).run()


if __name__ == "__main__":
    main()
