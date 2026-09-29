"""宠物医院 MCP 服务。

基于 MCP Python SDK 2.x（``mcp==2.0.0``）的 **无状态 Streamable HTTP** 服务，
协议版本 ``2026-07-28``，把现有 Go 宠物医院 REST API 暴露给 AI Agent。

本服务不包含任何业务数据，唯一业务后端是 Go REST 服务（通过 HTTP 调用）。
"""

from __future__ import annotations

__version__ = "0.1.0"

#: 实际使用的 MCP SDK 主版本（固定依赖见 pyproject.toml）
SDK_VERSION = "2.0.0"

#: 实际面向的 MCP 协议版本
PROTOCOL_VERSION = "2026-07-28"
