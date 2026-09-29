# Pet Hospital MCP Server（SDK 2.x · 无状态 Streamable HTTP）

把现有 **Go 宠物医院 REST API** 的能力，通过 **Model Context Protocol** 暴露给 AI Agent 的独立 Python 服务。

本服务本身**不存储任何业务数据**，唯一业务后端是仓库根目录的 Go 服务，MCP 服务仅通过 HTTP 调用它。

> 本阶段只提供 **一个工具：`list_pets`**。未实现任何阶段二工具（新增/修改病历等写操作均不支持）。

## 技术事实（实际版本）

| 项目 | 值 |
| --- | --- |
| Python | 3.11+（开发与测试环境为 3.13） |
| MCP SDK | **`mcp==2.0.0`**（Python SDK 2.x） |
| 协议版本 | **`2026-07-28`** |
| 服务端 API | **`MCPServer`**（`from mcp.server.mcpserver import MCPServer`） |
| 传输 | **无状态 Streamable HTTP**（`stateless_http=True`，`json_response=True`） |
| MCP 端点 | `POST http://127.0.0.1:8000/mcp` |
| 健康检查 | `GET http://127.0.0.1:8000/health` |
| 其他依赖 | `httpx`（上游调用）、`pydantic`（输入/输出模型） |

明确**不包含**：`mcp.server.fastmcp.FastMCP`、旧版 `initialize` 握手、
`Mcp-Session-Id`、会话存储/过期、`max_sessions`、有状态 SSE 会话恢复。
现代客户端使用 `server/discover` 发现能力，随后直接 `tools/list`、`tools/call`，
每个请求都携带 `Mcp-Protocol-Version: 2026-07-28` 头。

教学场景：不实现认证、权限、CORS / Origin 校验。

## 1. 先启动 Go REST API（必须）

MCP 服务只是适配层，Go 服务必须先运行（默认监听 `127.0.0.1:8080`）。
在仓库**根目录**（`go.mod` 所在目录，已安装 Go 工具链的环境）：

```bash
go run .
```

验证 Go 服务：

```bash
curl "http://127.0.0.1:8080/api/v1/pets?page=1&pageSize=2"
curl "http://127.0.0.1:8080/health"
```

## 2. 安装并启动 MCP 服务

在 `pet-hospital-mcp/` 目录：

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux
# source .venv/bin/activate

pip install -e ".[dev]"

# 启动（任选其一）
pet-hospital-mcp
# 或
python -m pet_hospital_mcp
```

启动日志（JSON）类似：

```json
{"timestamp":"2026-09-16T10:00:00.000+00:00","level":"INFO","logger":"pet_hospital_mcp.server","message":"starting MCP server","host":"127.0.0.1","port":8000,"mcp_endpoint":"/mcp","upstream_base_url":"http://127.0.0.1:8080","protocol_version":"2026-07-28"}
```

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PET_HOSPITAL_BASE_URL` | `http://127.0.0.1:8080` | Go REST API 根地址 |
| `MCP_HOST` | `127.0.0.1` | MCP 监听地址（默认仅本机） |
| `MCP_PORT` | `8000` | MCP 监听端口 |
| `PET_HOSPITAL_TIMEOUT` | `5.0` | 单次上游请求超时（秒） |
| `PET_HOSPITAL_MAX_RETRIES` | `2` | 上游超时/连接失败/5xx 的额外重试次数 |
| `PET_HOSPITAL_RETRY_BACKOFF` | `0.2` | 重试线性退避基数（秒） |

示例：

```powershell
$env:MCP_PORT="9000"
$env:PET_HOSPITAL_BASE_URL="http://127.0.0.1:8080"
pet-hospital-mcp
```

## 3. 健康检查

```bash
curl http://127.0.0.1:8000/health
```

```json
{
  "status": "ok",
  "service": "pet-hospital-mcp",
  "version": "0.1.0",
  "sdk_version": "mcp 2.0.0",
  "protocol_version": "2026-07-28",
  "transport": "stateless-streamable-http",
  "mcp_endpoint": "/mcp",
  "upstream_base_url": "http://127.0.0.1:8080"
}
```

该端点只反映 MCP 进程自身状态，不会调用上游 Go 服务。

## 4. 工具：`list_pets`

分页查询宠物档案，严格对应 `GET /api/v1/pets`。

### 参数（全部可选）

| 参数 | 类型 / 允许值 | 说明 |
| --- | --- | --- |
| `q` | string | 全文关键词 |
| `name` | string | 宠物姓名 |
| `ownerName` | string | 主人姓名 |
| `ownerPhone` | string | 主人电话 |
| `species` | `犬` `猫` `兔` `鸟` `仓鼠` `爬宠` `其他` | 物种 |
| `doctor` | string | 主治医生 |
| `disease` | string | 疾病/诊断关键词 |
| `status` | `待就诊` `就诊中` `住院中` `已康复` `慢性病随访` | 就诊状态 |
| `min` | number ≥ 0（拒绝 NaN/Infinity） | 总花费下限（元） |
| `max` | number ≥ 0 | 总花费上限；与 `min` 同时给出时必须 `min <= max` |
| `sortBy` | `id` `name` `ownerName` `species` `doctor` `disease` `status` `totalCost` `visitCount` `createdAt` `updatedAt` | 排序字段（默认 `createdAt`） |
| `order` | `asc` `desc` | 排序方向（默认 `desc`） |
| `page` | integer ≥ 1 | 页码（默认 1） |
| `pageSize` | integer 1–500 | 每页条数（默认 20） |

拒绝：未知字段、错误类型、越界值、`NaN`/`Infinity`。

### 成功返回（即 Go 响应的 `data`）

```json
{
  "items": [
    {
      "id": "PET-000001",
      "name": "旺财",
      "species": "犬",
      "records": null,
      "charges": null,
      "totalCost": 0
    }
  ],
  "total": 1,
  "page": 1,
  "pageSize": 20,
  "totalPages": 1,
  "totalCost": 0
}
```

`items` 中每个宠物的 `records` / `charges` 兼容 Go 的真实 JSON 表现：
可能是 `null`（无病历/消费），也可能是数组。

### 错误返回（统一结构，`isError=true`）

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "list_pets 参数校验失败，请检查入参取值与类型。",
    "details": { "issues": [ { "field": "pageSize", "type": "less_than_equal", "message": "..." } ] }
  }
}
```

错误码：`VALIDATION_ERROR`、`BACKEND_TIMEOUT`、`BACKEND_UNAVAILABLE`、
`BACKEND_API_ERROR`、`BACKEND_INVALID_RESPONSE`、`INTERNAL_ERROR`。
上游调用有超时与有限重试；httpx/Pydantic/SDK 的内部异常和堆栈不会暴露给客户端。

## 5. 验证步骤

### 5.1 使用 SDK 2.x 客户端（推荐）

需要 `mcp==2.0.0`。无状态流程是 **discover → list/call**，不调用 `initialize()`：

```python
import anyio
from mcp.client.streamable_http import streamable_http_client
from mcp.client.session import ClientSession


async def main() -> None:
    async with streamable_http_client("http://127.0.0.1:8000/mcp") as (rs, ws):
        async with ClientSession(rs, ws) as session:
            await session.discover()                      # 2026-07-28 现代握手
            print(session.protocol_version)               # 2026-07-28
            print([t.name for t in (await session.list_tools()).tools])
            result = await session.call_tool(
                "list_pets",
                {"species": "犬", "status": "就诊中", "page": 1, "pageSize": 10,
                 "min": 0, "max": 10000, "sortBy": "totalCost", "order": "desc"},
            )
            print(result.is_error)
            print(result.structured_content)


anyio.run(main)
```

无状态连接特征：`session.initialize_result is None`、
`session.discover_result is not None`；HTTP 响应中不出现 `Mcp-Session-Id`。

### 5.2 使用 MCP Inspector

Inspector 需以 **Streamable HTTP** 方式连接到：

```
http://127.0.0.1:8000/mcp
```

在工具面板选择 `list_pets`，填入参数即可调用；若 Inspector 版本较旧仍走
`initialize` 握手，请改用 5.1 的 SDK 2.x 客户端验证 2026-07-28 无状态流程。

### 5.3 原始 HTTP（现代无状态请求的必要头）

裸 JSON-RPC 还不够：2026-07-28 无状态请求必须携带协议/方法头，且请求体
`params._meta` 中需带协议版本标记。因此日常验证推荐直接使用 5.1 的官方客户端；
如需抓包，请以官方客户端实际发出的请求为准。

## 6. 单元测试

测试全部使用 `pytest` + `pytest-asyncio` + `httpx.MockTransport` /
`httpx2.ASGITransport`，**不会访问真实 Go 服务**。

在 `pet-hospital-mcp/` 目录运行（**唯一规定命令**）：

```bash
pytest -q
```

预期结果：

```
............................................................ [100%]
60 passed
```

覆盖范围：

- 正常调用时请求路径与全部 14 个过滤/排序/分页参数的正确转发；
- 输入校验失败（枚举、范围、未知字段、NaN/Infinity、错误类型、`min > max`）；
- 上游 4xx/5xx（含 5xx 重试）；
- 超时与连接异常（含重试次数）；
- 上游非法 JSON / 非对象 / 数据模型不匹配；
- 工具注册、工具名（snake_case）与 JSON Schema（枚举、范围、`additionalProperties:false`）；
- SDK 2.x 无状态连接：不发送 `initialize`、无 `Mcp-Session-Id`、
  `server/discover` + 2026-07-28 头、经 HTTP 发现与调用 `list_pets`；
- `/health` 端点。

## 目录结构

```
pet-hospital-mcp/
├── pyproject.toml
├── README.md
├── UPGRADE_PROMPT.md
├── src/
│   └── pet_hospital_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       ├── config.py
│       ├── server.py
│       ├── rest_client.py
│       ├── errors.py
│       ├── logging_config.py
│       └── tools/
│           ├── __init__.py
│           └── list_pets.py
└── tests/
    ├── conftest.py
    ├── test_rest_client.py
    ├── test_list_pets_tool.py
    ├── test_tool_registration.py
    ├── test_http_stateless.py
    └── test_logging.py
```

后续新增工具：在 `src/pet_hospital_mcp/tools/` 增加模块，复用
`rest_client` / `errors` / `logging_config` 的约定，再在 `server.py` 注册即可。
