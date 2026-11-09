# LLM 实践工作区

本仓库集中保存 LLM / MCP 相关的实践项目：从最简单的多轮流式对话，到完整的多轮 Agent 框架，再到 MCP Server 与 REST 服务。每个子目录均为可独立运行的完整项目。

## 目录结构

```text
LLM/
├── Agent/
│   ├── Practice01/          多轮流式对话（CLI，打字机输出）
│   ├── Practice02/          带 Function Calling 的工具调用 Agent（13 个工具）
│   └── xAgent/              完整 Agent 框架：TUI + MCP 客户端 + 技能系统 + 上下文压缩
├── AnythingLLMMCP/          AnythingLLM 工作区问答 MCP Server（Python，Streamable HTTP）
├── AnythingLLMServer/       AnythingLLM 文档上传单页（单文件 HTML）
├── PetHospitalMCP/          宠物医院 REST API（Go）+ 配套 Python MCP Server
├── docs/                    项目分析与优化报告
├── LICENSE                  MIT
└── README.md
```

## 项目概览

| 项目 | 语言 / 技术 | 一句话说明 |
|---|---|---|
| [Practice01](Agent/Practice01) | Python | 基于 OpenAI 兼容接口的多轮流式对话，逐字打字机输出 |
| [Practice02](Agent/Practice02) | Python | 在对话基础上加入 Function Calling，自动调用 13 个日期 / 文件工具 |
| [xAgent](Agent/xAgent) | Python + Textual | 核心层与 UI 层分离的 Agent 框架，支持 TUI/CLI、MCP 工具、技能注册、上下文压缩 |
| [AnythingLLMMCP](AnythingLLMMCP) | Python | 把本机 AnythingLLM 工作区问答暴露为 MCP 工具 `ask_workspace` |
| [AnythingLLMServer](AnythingLLMServer) | HTML/JS | 浏览器内上传文档到 AnythingLLM 并触发嵌入 |
| [PetHospitalMCP](PetHospitalMCP) | Go + Python | Go 标准库实现的本地宠物医院 REST API（内嵌网页），另有 Python MCP Server |

## 快速开始

### Agent 系列

```powershell
# 1. 复制配置模板（模板不含任何真实密钥）
Copy-Item Agent/Practice01/config.ini.example Agent/Practice01/config.ini
Copy-Item Agent/Practice02/config.ini.example Agent/Practice02/config.ini
Copy-Item Agent/xAgent/config.ini.example     Agent/xAgent/config.ini

# 2. 在 config.ini 中填入自己的 API Key，或使用环境变量（见下方安全说明）

# 3. 运行
python Agent/Practice01/chat.py
python Agent/Practice02/chat.py
python Agent/xAgent/main.py            # 默认 Textual TUI
python Agent/xAgent/main.py --ui console
```

详细工具清单与流程见 [Practice02/README.md](Agent/Practice02/README.md)。

### AnythingLLM MCP Server

```powershell
cd AnythingLLMMCP
pip install -r requirements.txt
$env:ANYTHINGLLM_API_KEY = "<你的 AnythingLLM API Key>"
python server.py        # 服务地址 http://127.0.0.1:8000/mcp
```

详见 [AnythingLLMMCP/README.md](AnythingLLMMCP/README.md)。

### 宠物医院 REST API

```bash
cd PetHospitalMCP
go run . -seed          # 编译并写入演示数据，浏览器打开 http://127.0.0.1:8080/
```

详见 [PetHospitalMCP/README.md](PetHospitalMCP/README.md)。

## 安全说明

- 仓库**不包含任何真实 API Key**。所有含密钥的本地配置文件（`config.ini`）已被 `.gitignore` 排除，仓库只保留 `config.ini.example` 模板。
- 推荐通过环境变量注入密钥：`XAGENT_LLM_API_KEY`、`XAGENT_SEARCH_API_KEY`、`ANYTHINGLLM_API_KEY`。
- xAgent 支持把密钥存入系统凭据管理器（Windows DPAPI），避免明文落盘：

  ```powershell
  python Agent/xAgent/main.py --set-key llm
  python Agent/xAgent/main.py --set-key search
  ```

- 若曾在任何地方（包括历史提交）暴露过密钥，请立即到服务商控制台轮换。

## 环境要求

- Python 3.10+（xAgent 需要 `textual`、`requests`、`mcp` 等依赖）
- Go 1.22+（仅 PetHospitalMCP）
- 默认使用 OpenAI 兼容的 SiliconFlow 接口，可替换为任意兼容 `chat/completions` 的服务

## 许可证

[MIT](LICENSE)。`PetHospitalMCP` 子项目自带一份独立的 [MIT LICENSE](PetHospitalMCP/LICENSE)。
