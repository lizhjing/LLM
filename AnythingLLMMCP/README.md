# AnythingLLM 工作区问答 MCP Server

基于 MCP 规范 **2026-07-28**（Streamable HTTP 传输、无状态）的 MCP 服务器，连接本机 AnythingLLM，提供工具 `ask_workspace`：在第一个工作区中检索文档并回答问题（`mode=query`，仅基于工作区文档内容，无相关内容时拒答）。

## 文件结构

| 文件 | 说明 |
|---|---|
| `server.py` | MCP 服务器（单文件，约 50 行） |
| `requirements.txt` | 依赖：`mcp>=2.0.0`（SDK v2，自动处理无状态、server/discover、resultType 等新规范） |
| `.trae/mcp.json` | Trae 项目级 MCP 配置，指向 `http://127.0.0.1:8000/mcp` |

## 环境要求

- Python 3.10+
- 本机 AnythingLLM 运行于 `http://localhost:3001`，且已配置 API Key

## 安装依赖

```powershell
pip install -r requirements.txt
```

## 环境变量（可选）

| 变量 | 默认值 |
|---|---|
| `ANYTHINGLLM_BASE_URL` | `http://localhost:3001` |
| `ANYTHINGLLM_API_KEY` | 代码内置的默认 Key |

## 启动服务器

**前台启动**（终端直接运行，日志可见）：

```powershell
python server.py
```

看到 `Uvicorn running on http://127.0.0.1:8000` 即启动成功。

**后台启动**（PowerShell，不占用当前终端）：

```powershell
Start-Process -WindowStyle Hidden python -ArgumentList "server.py" -WorkingDirectory "E:\PetHostpialServer"
```

服务地址：`http://127.0.0.1:8000/mcp`

## 停止服务器

**前台运行**：在终端按 `Ctrl+C`。

**后台运行**（PowerShell）：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'server\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## MCP 客户端接入

Trae 项目级配置已写在 `.trae/mcp.json`，在 Trae 的 MCP 面板中启用即可（需先启动服务器）。

其他 MCP 客户端（支持 Streamable HTTP）通用配置：

```json
{
  "mcpServers": {
    "anythingllm-workspace": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

## 验证

启动服务器后，用任意 MCP 客户端调用工具：

- 工具名：`ask_workspace`
- 参数：`{"question": "你的问题"}`
- 返回：工作区基于文档检索生成的回答（纯文本）

> 注意：若工作区尚未嵌入任何文档，query 模式会返回"无相关信息"的拒答，这是预期行为。请先在 AnythingLLM 中上传并嵌入文档。
