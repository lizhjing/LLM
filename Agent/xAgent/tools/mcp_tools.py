"""MCP 客户端:连接外部 MCP Server,把远程工具动态注册进 xAgent 工具注册表。

启动时读取当前项目目录下的 .mcp.json,支持 stdio 与 StreamableHTTP 两种协议。
每个 Server 独占一个后台线程运行 asyncio 事件循环,持有 MCP 会话的异步上下文;
工具调用通过 run_coroutine_threadsafe 转发,对上以同步 ToolSpec 形式参与统一的
Function Calling 流程。单个 Server 连接失败只记录问题,不影响其他工具。
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:  # mcp 2.x 命名
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # 兼容官方 SDK 的旧命名
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

from .registry import ToolSpec, text_result_limit

# 连接/发现阶段的默认超时(秒);工具调用可能较慢,单独放宽。
# 实际生效值来自 [mcp] 配置段,经 build_mcp_tools 注入
_CONNECT_TIMEOUT = 30.0
_CALL_TIMEOUT = 120.0


# ===== 配置解析 =====


def load_mcp_config(project_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读取 project_dir/.mcp.json,返回 (servers, problems)。

    文件不存在属于正常情况(未使用 MCP 功能),返回空配置。格式:
    {"mcpServers": {"名字": {"type": "stdio", "command": ..., "args": [...], "env": {...}}
                                 | {"type": "streamable_http", "url": "..."}}}
    """
    path = project_dir / ".mcp.json"
    if not path.is_file():
        return {}, []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"[MCP] {path} 解析失败,已忽略 MCP 功能: {exc}"]

    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}, [f"[MCP] {path} 缺少 'mcpServers' 字段,已忽略 MCP 功能"]

    problems: list[str] = []
    valid: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            problems.append(f"[MCP] Server '{name}' 配置不是对象,已跳过")
            continue
        kind = cfg.get("type")
        if kind is None:  # 省略 type 时按字段推断
            kind = "stdio" if "command" in cfg else "streamable_http" if "url" in cfg else None
        if kind == "stdio":
            if not cfg.get("command"):
                problems.append(f"[MCP] Server '{name}' 为 stdio 但缺少 command,已跳过")
                continue
        elif kind in ("streamable_http", "streamable-http", "http"):
            if not cfg.get("url"):
                problems.append(f"[MCP] Server '{name}' 为 streamable_http 但缺少 url,已跳过")
                continue
            kind = "streamable_http"
        else:
            problems.append(f"[MCP] Server '{name}' 的 type '{kind}' 不支持(仅 stdio/streamable_http),已跳过")
            continue
        valid[name] = {**cfg, "type": kind}
    return valid, problems


# ===== 连接管理 =====


class MCPServerConnection:
    """单个 MCP Server 的常驻连接:后台线程持有异步会话,同步接口对外。"""

    def __init__(
        self,
        name: str,
        cfg: dict[str, Any],
        connect_timeout: float = _CONNECT_TIMEOUT,
        call_timeout: float = _CALL_TIMEOUT,
    ) -> None:
        self.name = name
        self._cfg = cfg
        self._connect_timeout = float(connect_timeout)
        self._call_timeout = float(call_timeout)
        self._ready = threading.Event()
        # 用 threading.Event 作为停止信号:create 即存在,stop() 在任何阶段都可用,
        # 消除"asyncio.Event 尚未创建时 stop() 无效"的窗口(M5)
        self._stop_event = threading.Event()
        self._session: ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    # -- 生命周期 --

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._serve, name=f"mcp-{self.name}", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(self._connect_timeout):
            err = self._error or "连接超时"
            self.stop()
            raise RuntimeError(f"连接超时({self._connect_timeout:.0f}s): {err}")
        if self._error:
            raise RuntimeError(self._error)

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop  # 供 _run() 提交协程使用
        asyncio.set_event_loop(loop)

        async def establish(stack: AsyncExitStack, mode: str) -> ClientSession:
            """建立传输与会话,并按指定方式完成握手(带超时保护)。"""
            if self._cfg["type"] == "stdio":
                env = {**os.environ, **(self._cfg.get("env") or {})}
                params = StdioServerParameters(
                    command=self._cfg["command"],
                    args=list(self._cfg.get("args") or []),
                    env=env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                streams = await stack.enter_async_context(
                    streamable_http_client(self._cfg["url"])
                )
                # mcp 2.x 产出 (read, write) 二元组;官方版本为三元组(多 get_session_id)
                read, write = streams[0], streams[1]
            session = await stack.enter_async_context(ClientSession(read, write))
            if mode == "discover":
                if not hasattr(session, "discover"):
                    # M8:SDK 版本预判,避免误导性的 AttributeError
                    raise RuntimeError("当前 mcp SDK 不支持 discover 握手")
                await session.discover()  # 无状态 Streamable HTTP(协议 2026-07-28)
            else:
                await session.initialize()  # 标准握手
            return session

        async def main() -> None:
            # 握手顺序:先标准 initialize;失败(如无状态 2026-07-28 服务器不支持)则
            # 丢弃本次传输、重建后改用 discover。stdio 服务器只走标准握手。
            modes = ("initialize",) if self._cfg["type"] == "stdio" else ("initialize", "discover")
            session: ClientSession | None = None
            last_err: Exception | None = None
            async with AsyncExitStack() as stack:
                for mode in modes:
                    try:
                        session = await asyncio.wait_for(
                            establish(stack, mode), timeout=self._connect_timeout
                        )
                        break
                    except Exception as exc:
                        last_err, session = exc, None
                        try:
                            await stack.aclose()  # 丢弃本次传输,下次迭代重建
                        except Exception:
                            pass  # 传输可能已随 TaskGroup 异常终结
                if session is None:
                    raise RuntimeError(f"握手失败(initialize/discover 均不可用): {last_err}")
                self._session = session
                self._ready.set()
                # 保持上下文开启,直到 stop() 触发停止信号
                await loop.run_in_executor(None, self._stop_event.wait)
                self._session = None

        try:
            loop.run_until_complete(main())
        except Exception as exc:  # 会话中断/进程退出等
            if self._loop is loop:  # 防止重建后旧线程覆盖新状态
                self._error = str(exc)
        finally:
            if self._loop is loop:
                self._ready.set()  # 解除 start() 的等待
            loop.close()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _restart(self) -> None:
        """调用超时后放弃旧连接并重建(旧线程为 daemon,自行消亡)。"""
        logger.warning("MCP Server '%s' 调用超时,重建连接", self.name)
        self._stop_event.set()
        self._ready.clear()
        self._error = None
        self._session = None
        self._loop = None
        self._thread = None
        self.start()

    # -- 调用 --

    def list_tools(self) -> list[Any]:
        return self._run(self._session.list_tools())

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return self._run(self._session.call_tool(tool_name, arguments))

    def _run(self, coro: Any) -> Any:
        if self._session is None or self._loop is None or self._loop.is_closed():
            raise RuntimeError("MCP 会话不可用")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(self._call_timeout)
        except TimeoutError:
            # 运行中的协程无法强杀:放弃旧连接并整体重建,避免僵尸会话继续被复用(M6)
            self._restart()
            raise RuntimeError(
                f"MCP 调用超时({self._call_timeout:.0f}s),连接已重置,请重试"
            ) from None


# ===== 结果转换 =====


def result_to_text(result: Any) -> str:
    """把 CallToolResult 转为文本,覆盖文本内容、结构化数据与错误信封。"""
    prefix = "MCP 工具返回错误:\n" if getattr(result, "isError", False) else ""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:  # ImageContent / EmbeddedResource 等非文本块
            parts.append(f"[非文本内容块: {type(item).__name__}]")
    if not parts:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
    if not parts:
        parts.append("(工具返回空结果)")
    text = prefix + "\n".join(parts)
    limit = text_result_limit()
    if len(text) > limit:
        text = text[:limit] + f"\n...(已截断:原始返回共 {len(text)} 字符)"
    return text


# ===== 工具发现与注册 =====


def _sanitize(name: str) -> str:
    """LLM 工具名只允许字母/数字/下划线/连字符,其余字符替换为下划线。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _connect_one(
    server_name: str,
    cfg: dict[str, Any],
    connect_timeout: float,
    call_timeout: float,
) -> tuple[str, MCPServerConnection | None, list[tuple[str, str, str, dict[str, Any]]]]:
    """连接单个 Server 并发现工具。

    返回 (note, conn, tools):note 为启动信息(连接失败时即错误说明);
    tools 为 (exposed_raw, original_name, description, schema) 四元组列表。
    """
    conn = MCPServerConnection(server_name, cfg, connect_timeout, call_timeout)
    try:
        conn.start()
        tools = conn.list_tools()
    except Exception as exc:
        logger.warning("MCP Server '%s' 连接失败: %s", server_name, exc)
        conn.stop()
        return (
            f"[MCP] Server '{server_name}' 连接失败: {exc}(其余工具不受影响)",
            None,
            [],
        )
    prefix = _sanitize(server_name)
    entries = []
    for tool in tools.tools:
        # mcp 2.x 为 input_schema(snake_case),官方版本为 inputSchema
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        schema = schema if isinstance(schema, dict) else {}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        desc = (tool.description or "").strip() or "(无描述)"
        entries.append(
            (f"{prefix}__{_sanitize(tool.name)}", tool.name, f"[MCP:{server_name}] {desc}", schema)
        )
    return (f"[MCP] Server '{server_name}' 已连接,注册 {len(entries)} 个工具", conn, entries)


@dataclass
class MCPServerInfo:
    """单个 MCP Server 的加载结果(供界面侧栏/面板展示)。"""

    name: str
    connected: bool = False
    tools: list[str] = field(default_factory=list)  # 暴露给 LLM 的工具名(含前缀)
    error: str = ""  # 连接失败原因;成功时为空


@dataclass
class MCPLoadResult:
    """一次 MCP 加载的完整结果。"""

    specs: list[ToolSpec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 启动信息(连接成功/失败明细)
    connections: list[MCPServerConnection] = field(default_factory=list)  # 退出时清理
    servers: list[MCPServerInfo] = field(default_factory=list)  # 面向界面展示的结构化信息


def build_mcp_tools(
    project_dir: Path,
    connect_timeout: float | None = None,
    call_timeout: float | None = None,
) -> MCPLoadResult:
    """连接 .mcp.json 中的全部 Server,返回可注册的 ToolSpec 与结构化加载结果。

    超时参数缺省时用模块默认值;来自 [mcp] 配置段。Server 之间并发连接(M9),
    单个失败不影响其余。
    """
    ct = _CONNECT_TIMEOUT if connect_timeout is None else float(connect_timeout)
    clt = _CALL_TIMEOUT if call_timeout is None else float(call_timeout)
    servers, problems = load_mcp_config(project_dir)
    notes = list(problems)
    if not servers:
        return MCPLoadResult(specs=[], notes=notes, connections=[], servers=[])

    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        outcomes = list(
            pool.map(lambda item: _connect_one(*item, ct, clt), servers.items())
        )

    specs: list[ToolSpec] = []
    used_names: set[str] = set()
    conns: list[MCPServerConnection] = []
    infos: list[MCPServerInfo] = []

    for (server_name, _cfg), (note, conn, entries) in zip(servers.items(), outcomes):
        notes.append(note)
        if conn is None:
            infos.append(MCPServerInfo(name=server_name, connected=False, error=note))
            continue
        conns.append(conn)
        count = 0
        for exposed, original, desc, schema in entries:
            while exposed in used_names:  # 兜底:清洗后仍冲突则追加序号
                exposed += "_2"
            used_names.add(exposed)
            specs.append(
                ToolSpec(name=exposed, description=desc, parameters=schema,
                         handler=_make_handler(conn, original))
            )
            count += 1
        infos.append(
            MCPServerInfo(
                name=server_name,
                connected=True,
                tools=[s.name for s in specs[-count:]] if count else [],
            )
        )
        _ = server_name

    if conns:
        atexit.register(_stop_all, conns)
    return MCPLoadResult(specs=specs, notes=notes, connections=conns, servers=infos)


def _make_handler(conn: MCPServerConnection, tool_name: str):
    """生成闭包:把 LLM 的参数字典转发给对应 Server 的原始工具名。"""

    def handler(arguments: dict[str, Any]) -> str:
        return result_to_text(conn.call_tool(tool_name, arguments or {}))

    return handler


def _stop_all(conns: list[MCPServerConnection]) -> None:
    for conn in conns:
        conn.stop()
