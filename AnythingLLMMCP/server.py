"""AnythingLLM MCP Server（MCP 规范 2026-07-28，Streamable HTTP 传输）。

协议层细节（无状态、server/discover、Mcp-Method 头、resultType）由 mcp SDK v2 自动处理。
对外暴露四类能力：工作区问答、工作区列举与创建、本地文档上传并向量化。
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import requests
from mcp.server import MCPServer

BASE_URL = os.getenv("ANYTHINGLLM_BASE_URL", "http://localhost:3001")
API_KEY = os.getenv("ANYTHINGLLM_API_KEY", "")

# 读取本地文件的上限，避免超大文件整块进内存
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024

mcp = MCPServer("anythingllm-workspace")


def _missing_key() -> str | None:
    """未配置密钥时返回统一提示，调用方据此在入口处直接返回。"""
    if API_KEY.strip():
        return None
    return "未配置密钥：请设置环境变量 ANYTHINGLLM_API_KEY 后重启本服务"


def _call_api(method: str, path: str, payload: dict | None = None) -> dict:
    """调用 AnythingLLM 管理 API，把各类失败转成结构化中文错误文本。

    Returns:
        成功时为 ``{"ok": True, "data": <解析后的 JSON>}``；
        失败时为 ``{"ok": False, "error": "可读错误信息"}``，绝不抛出异常。
    """
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    request = urllib.request.Request(
        BASE_URL.rstrip("/") + "/api" + path, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        hint = f"（{detail}）" if detail else ""
        return {"ok": False, "error": f"AnythingLLM 返回 HTTP {exc.code}{hint}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"无法连接 AnythingLLM（{exc.reason}），请确认服务已启动"}
    except TimeoutError:
        return {"ok": False, "error": "AnythingLLM 请求超时，请稍后重试"}
    try:
        return {"ok": True, "data": json.loads(raw)}
    except json.JSONDecodeError:
        return {"ok": False, "error": "AnythingLLM 返回了无法解析为 JSON 的内容"}


def _get_workspaces() -> tuple[list[dict], str | None]:
    """获取工作区列表，返回 (列表, 错误信息)。"""
    result = _call_api("GET", "/v1/workspaces")
    if not result["ok"]:
        return [], result["error"]
    workspaces = result["data"].get("workspaces")
    if not isinstance(workspaces, list):
        return [], "AnythingLLM 响应缺少 workspaces 列表"
    return workspaces, None


def _upload_document(file_path: str) -> tuple[str | None, str | None]:
    """把本地文件 multipart 上传到 AnythingLLM，返回 (location, 错误信息)。"""
    path = Path(file_path)
    if not path.is_file():
        return None, f"文件不存在或不是普通文件：{file_path}"
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"读取文件失败：{exc}"
    if len(data) > _MAX_UPLOAD_BYTES:
        return None, f"文件超过 {_MAX_UPLOAD_BYTES} 字节上限，请先拆分"
    try:
        response = requests.post(
            BASE_URL.rstrip("/") + "/api/v1/document/upload",
            headers={"Authorization": f"Bearer {API_KEY}"},
            files={"file": (path.name, data, "application/octet-stream")},
            timeout=120,
        )
    except requests.RequestException as exc:
        return None, f"上传请求失败：{exc}"
    if response.status_code >= 400:
        return None, f"上传失败：AnythingLLM 返回 HTTP {response.status_code} {response.text[:200]}"
    try:
        documents = response.json().get("documents") or []
        location = documents[0]["location"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None, "上传失败：AnythingLLM 响应中缺少 documents[0].location"
    return str(location), None


@mcp.tool()
def ask_workspace(question: str) -> str:
    """在第一个工作区中检索文档并回答问题（仅基于工作区文档内容，无相关内容时会拒答）"""
    missing = _missing_key()
    if missing:
        return missing
    workspaces, error = _get_workspaces()
    if error:
        return error
    if not workspaces:
        return "AnythingLLM 中还没有任何工作区，请先创建一个工作区并上传文档"
    slug = workspaces[0]["slug"]
    result = _call_api("POST", f"/v1/workspace/{slug}/chat", {"message": question, "mode": "query"})
    if not result["ok"]:
        return result["error"]
    data = result["data"]
    if data.get("error"):
        return f"AnythingLLM 返回错误: {data['error']}"
    return data.get("textResponse") or "AnythingLLM 未返回回答内容"


@mcp.tool()
def list_workspaces() -> str:
    """列出 AnythingLLM 中的全部工作区及其文档数量，用于选择后续上传或提问的目标工作区"""
    missing = _missing_key()
    if missing:
        return missing
    workspaces, error = _get_workspaces()
    if error:
        return error
    if not workspaces:
        return "AnythingLLM 中还没有任何工作区"
    lines = []
    for item in workspaces:
        documents = item.get("documents")
        count = len(documents) if isinstance(documents, list) else 0
        lines.append(f"- {item.get('name')}（slug: {item.get('slug')}，文档数: {count}）")
    return "\n".join(lines)


@mcp.tool()
def create_workspace(name: str) -> str:
    """创建一个新的 AnythingLLM 工作区，返回其名称与 slug（slug 供上传文档时指定目标工作区）"""
    missing = _missing_key()
    if missing:
        return missing
    if not name.strip():
        return "参数错误：name 不能为空"
    result = _call_api("POST", "/v1/workspace/new", {"name": name.strip()})
    if not result["ok"]:
        return result["error"]
    workspace = result["data"].get("workspace") or {}
    if not workspace.get("slug"):
        return f"创建工作区失败：{result['data'].get('message') or '响应缺少 slug'}"
    return f"已创建工作区：{workspace.get('name')}（slug: {workspace['slug']}）"


@mcp.tool()
def upload_document(file_path: str, workspace_slug: str = "") -> str:
    """上传本地文件到 AnythingLLM 并向量化；workspace_slug 省略时使用第一个工作区。

    参数：
        file_path: 本地文件路径
        workspace_slug: 目标工作区 slug，可从 list_workspaces 获取；可选
    """
    missing = _missing_key()
    if missing:
        return missing
    workspaces, error = _get_workspaces()
    if error:
        return error
    if workspace_slug.strip():
        slug = workspace_slug.strip()
        if not any(item.get("slug") == slug for item in workspaces):
            available = "、".join(str(item.get("slug")) for item in workspaces) or "（无）"
            return f"工作区不存在：{slug}；可用 slug：{available}"
    elif workspaces:
        slug = workspaces[0]["slug"]
    else:
        return "AnythingLLM 中还没有任何工作区，请先用 create_workspace 创建"

    location, error = _upload_document(file_path)
    if error:
        return error

    embed = _call_api(
        "POST",
        f"/v1/workspace/{urllib.parse.quote(slug, safe='')}/update-embeddings",
        {"adds": [location]},
    )
    if not embed["ok"]:
        return f"文档已上传（location: {location}），但向量化失败：{embed['error']}"
    if embed["data"].get("success") is False:
        return f"文档已上传（location: {location}），但向量化未成功：{embed['data'].get('message')}"
    return f"已上传并完成向量化：{location}（工作区 slug: {slug}）"


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8000,
        stateless_http=True,
    )
