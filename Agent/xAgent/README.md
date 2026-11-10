# xAgent

核心层与 UI 层分离的本地 Agent 框架。基于 OpenAI 兼容接口，支持多轮对话、Function Calling、MCP 工具接入、技能（Skill）动态注册，以及上下文压缩与历史检索。

## 特性

- **双层架构**：`core/`（配置、Agent 循环、LLM 客户端、技能注册、上下文压缩、密钥存储）与 `ui/`（Textual TUI / 控制台）严格分离，便于扩展 Web 等其他界面
- **两种界面**：默认 Textual TUI（流式输出 + 状态栏 Token 占用），也可切换传统命令行界面
- **工具系统**：日期、文件、CLI、Web 抓取、上下文检索等内置工具，支持路径逃逸防护、SSRF 拦截、命令白名单与统一超时
- **MCP 客户端**：从启动目录的 `.mcp.json` 读取配置，以 `server__tool` 前缀动态注册工具；单服务器失败不影响其他工具
- **技能系统**：扫描 `.agents/skills` 下的 `SKILL.md`（YAML Frontmatter），变更后自动刷新系统提示，无需重启
- **上下文压缩**：接近窗口上限时归档原始上下文到 `.agents/context/`，生成摘要后继续对话；配合 `recallContext` 工具做本地全文检索
- **密钥安全**：支持环境变量、Windows 凭据管理器（DPAPI）与配置文件三种方式，避免明文提交

## I 安装依赖

要求 Python 3.10+。建议使用独立虚拟环境（`dist/`、`build/`、`.venv/` 均已被 `.gitignore` 忽略）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` 中 `textual`、`requests`、`mcp` 为必需；`trafilatura` 为可选，仅 `fetchArticle` 工具需要。

## 运行

```powershell
python main.py                  # 默认 Textual TUI
python main.py --ui console     # 命令行界面
python main.py --set-key llm    # 录入 LLM API Key 到系统凭据管理器
python main.py --clear-key llm  # 删除凭据
```

首次运行前，把 `config.ini.example` 复制为 `config.ini` 并填写 `[llm]`、`[search]` 配置（该文件已被 `.gitignore` 忽略）。

需要使用 MCP 工具时，把 `.mcp.json.example` 复制为 `.mcp.json`（放在启动工作目录），按注释填入 `stdio` 或 `streamable_http` 服务器；`.mcp.json` 已被 `.gitignore` 忽略。

## 项目说明（AGENTS.md）

在启动工作目录放一个 `AGENTS.md`，其内容会在会话建立时**一次性注入为 system 消息**（位于 system 提示之后），用于向 Agent 交代项目约定（如"回答用中文""提交前先跑测试"）。

- 文件名可通过 `[agent] agents_md_name` 配置，默认 `AGENTS.md`；找不到时回退尝试 `AGENT.md`，两者都没有则静默跳过。
- 内容超过 `[agent] agents_md_max_chars`（默认 32768 字符）时按上限截断并注明。
- 会话恢复、重置、上下文压缩后该说明都会重新就位，跨轮次持续生效。

## 目录

```text
xAgent/
├── main.py              程序入口（装配配置、工具、Agent、界面）
├── config.ini.example   配置模板（不含密钥）
├── JAGENT.spec          PyInstaller 打包配置
├── core/                配置、Agent 循环、LLM 客户端、技能注册、上下文压缩、密钥存储、日志
├── tools/               工具实现与注册表
├── ui/                  Textual TUI / 控制台界面
└── .agents/skills/      技能目录（每个技能一个子目录，含 SKILL.md）
```

## 密钥配置优先级

1. 环境变量 `XAGENT_LLM_API_KEY` / `XAGENT_SEARCH_API_KEY`
2. 系统凭据管理器（`--set-key`）
3. `config.ini` 中的 `api_key`（明文，不推荐）
