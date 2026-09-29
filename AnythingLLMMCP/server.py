"""AnythingLLM 工作区问答 MCP Server（MCP 规范 2026-07-28，Streamable HTTP 传输）。

协议层细节（无状态、server/discover、Mcp-Method 头、resultType）由 mcp SDK v2 自动处理。
"""
import json
import os
import urllib.request

from mcp.server import MCPServer

BASE_URL = os.getenv("ANYTHINGLLM_BASE_URL", "http://localhost:3001")
API_KEY = os.getenv("ANYTHINGLLM_API_KEY", "PBPKTTC-X52M23H-G6Z4M31-PRARZMF")

mcp = MCPServer("anythingllm-workspace")


def _call_api(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + "/api" + path,
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


@mcp.tool()
def ask_workspace(question: str) -> str:
    """在第一个工作区中检索文档并回答问题（仅基于工作区文档内容，无相关内容时会拒答）"""
    slug = _call_api("GET", "/v1/workspaces")["workspaces"][0]["slug"]
    r = _call_api("POST", f"/v1/workspace/{slug}/chat", {"message": question, "mode": "query"})
    if r.get("error"):
        return f"AnythingLLM 返回错误: {r['error']}"
    return r["textResponse"]


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8000,
        stateless_http=True,
    )
