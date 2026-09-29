# MCP Server 开发计划：AnythingLLM 工作区问答

## Context
- 目标目录 `e:\PetHostpialServer` 为空目录，全新项目
- 需求：用 Python 开发 MCP Server（HTTP 传输，非 stdio），遵循 **2026-07-28 MCP 规范**，只实现一个功能：在本机 AnythingLLM 的**第一个工作区**提取信息回答问题
- 约束：尽量简单、无冗余设计、不过度开发

## 已验证事实（本次调研结论）

### 1. MCP 2026-07-28 规范要点
- 无状态协议核心：移除 `initialize` 握手与 `Mcp-Session-Id`，每个请求经 `_meta` 自带 protocolVersion / clientInfo / clientCapabilities
- 服务端必须实现 `server/discover` RPC；Streamable HTTP POST 需带 `Mcp-Method` / `Mcp-Name` 头；结果需 `resultType` 字段；list 响应带 ttlMs/cacheScope
- **MCP Python SDK v2**（`mcp>=2.0.0`，2026-07-28 随规范发布，当前最新 2.2.0）已完整支持：`from mcp.server import MCPServer` + `@mcp.tool()` 装饰器，上述协议细节全部由 SDK 自动处理，同一个 HTTP app 同时兼容新旧协议版本

### 2. AnythingLLM API（已实测连通，API key 有效）
- `GET /api/v1/workspaces` → 当前仅 1 个工作区：`我的工作区`，slug=`504f093e-423a-40f2-90e3-72d89f0f019b`
- `POST /api/v1/workspace/{slug}/chat`，body `{"message": "...", "mode": "query"}` → 响应 `{textResponse, sources[{title, chunk}], error}`
- `mode="query"`：仅基于工作区文档检索回答、不回忆聊天历史、无相关来源时拒答 —— 正是"提取信息"语义
- 认证：`Authorization: Bearer <API_KEY>`
- API 文档规范内嵌于 `http://localhost:3001/api/docs/swagger-ui-init.js`（无独立 openapi.json）

## 实现方案（2 个文件，约 70 行）

### 文件 1：`server.py`（单文件）
```python
import os
import json
import urllib.request
from mcp.server import MCPServer

BASE_URL = os.getenv("ANYTHINGLLM_BASE_URL", "http://localhost:3001")
API_KEY  = os.getenv("ANYTHINGLLM_API_KEY",  "PBPKTTC-X52M23H-G6Z4M31-PRARZMF")

mcp = MCPServer("anythingllm-workspace")

def _call_api(method: str, path: str, payload: dict | None = None) -> dict:
    # urllib 封装：Bearer 头 + JSON 编解码 + 非 2xx 抛错

@mcp.tool()
def ask_workspace(question: str) -> str:
    """在第一个工作区中检索文档并回答问题（仅基于工作区内容，无相关内容时说明无法回答）"""
    slug = _call_api("GET", "/v1/workspaces")["workspaces"][0]["slug"]
    r = _call_api("POST", f"/v1/workspace/{slug}/chat",
                  {"message": question, "mode": "query"})
    return r["textResponse"]

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000)  # run() 签名以安装的 SDK 2.x 实际为准
```

### 文件 2：`requirements.txt`
```
mcp>=2.0.0
```

## 设计决策（避免过度设计）
| 决策 | 理由 |
|---|---|
| 仅 1 个工具 `ask_workspace`，参数仅 `question` | 满足"只实现一个功能"；不要 sessionId/reset/attachments 等可选参数 |
| 第一个工作区每次调用实时获取 | 无状态，符合 2026-07-28 规范，不做缓存不做启动时快照 |
| `mode` 固定为 `"query"` | "提取信息"语义即 query 模式；不暴露 mode 参数 |
| HTTP 客户端用标准库 `urllib` | 零新增依赖；SDK v2 的同步 `def` 工具自动在线程池运行，不阻塞事件循环 |
| 协议层（server/discover、Mcp-Method 头、resultType、无状态）不手写 | 全部由 SDK v2 自动实现 |
| 工具返回仅 `textResponse` 纯文本 | 不返回 sources/chunk，节约 token |

## 实施步骤
1. `pip install "mcp[cli]>=2.0.0"`，并用 `pip show mcp` + 源码确认 v2 `run()` 的确切签名与 `MCPServer` 导入路径
2. 编写 `server.py` 与 `requirements.txt`
3. 按验证方案实测

## 验证方案
1. 启动：`python server.py`，监听 `http://127.0.0.1:8000/mcp`
2. 写一个约 10 行的临时测试脚本，用 SDK 官方 `Client("http://127.0.0.1:8000/mcp")` 直接 `call_tool("ask_workspace", {"question": "..."})`，确认返回工作区答案（测完删除脚本）
3. 无状态验证：不握手直接 `tools/call` 成功（SDK Client 天然如此）
4. curl 裸 JSON-RPC 抽查 `tools/list` 响应含 `resultType` 与缓存元数据（可选）
