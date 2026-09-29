# UPGRADE_PROMPT：从 SDK 1.x FastMCP 迁移到 SDK 2.0.0 MCPServer（无状态）

> 用途：当仓库中存在基于 MCP Python SDK 1.x / FastMCP 的旧服务时，把下面这段
> 提示词作为迁移任务说明使用。本文中的 API 事实均基于 **`mcp==2.0.0`**
> 安装包源码核对（不是凭 1.x 经验猜测），对应协议版本 **2026-07-28**。

## 角色与目标

你是精通 MCP 2026-07-28 与 Python SDK 2.x 的工程师。把现有 SDK 1.x 的
`FastMCP` 服务迁移为基于 `MCPServer` 的**无状态 Streamable HTTP** 服务。
不得兼容或保留 1.x 的 FastMCP 代码路径。

## 必须遵守的硬约束

1. 依赖固定 `mcp==2.0.0`，Python 3.11+；HTTP 客户端用 `httpx`，模型用 `pydantic`。
2. 只能使用：
   - `from mcp.server.mcpserver import MCPServer`（`mcp.server` 也重导出该类）；
   - 结果类型 `from mcp_types import CallToolResult, TextContent`。
3. 禁止使用 / 导入 `mcp.server.fastmcp.FastMCP`。
4. 禁止实现：旧版 `initialize` 握手、`Mcp-Session-Id`、会话存储、会话过期、
   `max_sessions`、有状态 SSE 恢复。
5. 传输装配必须同时设置：

   ```python
   server.run(
       transport="streamable-http",
       host="127.0.0.1",
       port=8000,
       streamable_http_path="/mcp",
       stateless_http=True,
       json_response=True,
   )
   ```

   或在进程内 / ASGI 场景调用
   `server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=...)`。
   传输参数**不能**传给 `MCPServer(...)` 构造器。
6. 默认只监听 `127.0.0.1`；`MCP_HOST` / `MCP_PORT` 通过环境变量可配置；
   保留 `GET /health`。
7. 教学/内网场景不实现认证、权限、CORS、Origin 校验。

## 1.x → 2.x 关键差异（迁移要点）

### 服务器构造与工具注册

- 旧：`mcp = FastMCP("name")` + `@mcp.tool()`。
- 新：

  ```python
  from mcp.server.mcpserver import MCPServer

  server = MCPServer(name="pet-hospital-mcp", version="0.1.0")

  @server.tool()                       # 注意必须带括号；@server.tool 会 TypeError
  async def my_tool(x: int | None = None) -> CallToolResult:
      ...

  # 等价写法：server.add_tool(fn, name="my_tool", description="...")
  ```

- 每个普通函数参数成为 JSON Schema 的顶层属性；不能把单个 Pydantic model
  入参“拍平”为多个属性。
- 需要请求上下文时，声明一个 `Context` 类型参数（自动注入、不进 schema）：

  ```python
  from mcp.server.mcpserver import Context

  async def my_tool(x: Any = None, ctx: Context | None = None) -> CallToolResult: ...
  ```

### 错误处理（不要靠抛异常）

- 2.x 中工具函数抛出的普通异常会被包装成
  `ToolError("Error executing tool <name>: ...")`，最终变成
  `CallToolResult(is_error=True, content=[TextContent(str(e))])`：
  **会被加上固定前缀、且丢失 `structured_content`**，无法承载统一错误结构。
- 需要统一错误信封时，**显式构造并返回** `CallToolResult`：

  ```python
  CallToolResult(
      content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
      structured_content={"error": {"code": "...", "message": "...", "details": {}}},
      is_error=True,
  )
  ```

  2.x 仍使用 `is_error`（Python snake_case；线上序列化为 `isError`）。
- 工具返回类型标注为 `CallToolResult`（且不用 `Annotated[...]` 附加输出模型）
  时，SDK 不会再对 `structured_content` 做输出模型校验，结果原样透传。
- `MCPError` 会变成 JSON-RPC 协议层错误（客户端在业务结果之外收到 RPC error），
  不要用它表达“工具业务失败”。

### 严格输入校验与对外 Schema 的正确姿势

- SDK 根据签名自动生成的参数模型基类是
  `ArgModelBase(BaseModel)`，默认 `extra="ignore"`，且在函数体执行**之前**
  就完成校验；这意味着：
  - 未知字段会被静默丢弃；
  - 类型/枚举错误在函数体外被拦截，拿不到“统一错误结构”的机会。
- 迁移方案（本仓库已采用）：
  1. 工具函数形参全部声明为宽松的 `Any = None`，保证任何输入都能进入函数体；
  2. 函数体从 `ctx` 取**原始 arguments**，用自定义严格 Pydantic 模型
     （`ConfigDict(extra="forbid", strict=True)`，浮点用
     `Field(ge=0, allow_inf_nan=False)`）重新校验，失败时返回统一
     `VALIDATION_ERROR`；
  3. 注册后把对外 schema 替换为严格模型生成的 schema：

     ```python
     server.add_tool(fn, name="list_pets", description=...)
     tool = server._tool_manager.get_tool("list_pets")
     tool.parameters = ListPetsInput.model_json_schema()
     ```

- 2026-07-28 现代无状态路径下，类型化 `ctx.request_context.request` 可能为
  `None`；原始 JSON-RPC 参数在 `ctx.request_context.params`（Mapping）里，
  取参数时两条路径都要兼容。

### 无状态 Streamable HTTP 的事实

- `stateless_http=True`：服务端不为每个 POST 分配会话，**不返回也不要求**
  `Mcp-Session-Id`；`json_response=True`：每个 POST 直接回单个 JSON 对象。
- 现代请求按 **HTTP 头路由**，每个请求需要：
  - `Mcp-Protocol-Version: 2026-07-28`
  - `Mcp-Method` 与 body 的 JSON-RPC method 一致（如 `server/discover`、
    `tools/list`、`tools/call`）
  - `tools/call` 还需 `Mcp-Name` 等于工具名
  - body 的 `params._meta` 需携带协议版本标记与客户端能力
  - 裸发旧版 JSON-RPC（不带这些头）不会走 2026-07-28 现代路径。
- 自定义 HTTP 路由（如健康检查）：

  ```python
  from starlette.responses import JSONResponse

  @server.custom_route("/health", methods=["GET"])
  async def health(request) -> JSONResponse:
      return JSONResponse({"status": "ok"})
  ```

### 客户端（验证迁移结果）

```python
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession

async with streamable_http_client("http://127.0.0.1:8000/mcp") as (rs, ws):
    async with ClientSession(rs, ws) as session:
        await session.discover()          # 不要调用 initialize()
        assert session.protocol_version == "2026-07-28"
        assert session.initialize_result is None
        assert session.discover_result is not None
        await session.list_tools()
        await session.call_tool("list_pets", {"page": 1})
```

- `ClientSession` 进入 context **不会**自动握手；无状态流程显式调用
  `await session.discover()`。
- 进程内 ASGI 测试用 SDK 内置的 `httpx2`（不是业务依赖 `httpx`）：

  ```python
  import httpx2
  transport = httpx2.ASGITransport(app=app)
  client = httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000")
  ```

  注意两点实测坑：
  1. httpx2 异步 client 的 event hooks 必须是 **async 函数**；
  2. `ASGITransport` 不触发 Starlette lifespan，测试里需手动进入
     `async with server.session_manager.run(): ...`（真实 uvicorn 启动无需关心）；
  3. localhost 默认开启 DNS rebinding 保护，测试基址要带端口
     （如 `http://127.0.0.1:8000`）以匹配 `127.0.0.1:*` 白名单。

## 错误分类与对外承诺

上游 HTTP 调用必须：设置超时、有限重试（仅超时/连接错误/5xx，4xx 不重试），
并把异常映射为稳定错误码，不泄露底层库异常与堆栈：

`VALIDATION_ERROR` / `BACKEND_TIMEOUT` / `BACKEND_UNAVAILABLE` /
`BACKEND_API_ERROR` / `BACKEND_INVALID_RESPONSE` / `INTERNAL_ERROR`。

统一信封：

```json
{ "error": { "code": "ERROR_CODE", "message": "可读信息", "details": {} } }
```

## 验收清单

- [ ] 全仓库无 `FastMCP`、无会话/`Mcp-Session-Id`/`initialize` 服务端实现；
- [ ] 固定 `mcp==2.0.0`，使用 `MCPServer`，协议 `2026-07-28`；
- [ ] `pytest -q` 全绿，测试不访问真实上游；
- [ ] 无状态客户端 `discover()` 后可 `tools/list` 与 `tools/call`，
      响应中无 `Mcp-Session-Id`；
- [ ] 所有非法输入与上游异常均返回统一错误信封且 `isError=true`；
- [ ] `/health` 可用；README 写清安装、启动、环境变量、端点与验证步骤。
