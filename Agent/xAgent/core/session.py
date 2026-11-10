"""会话持久化与导出(纯核心层,与界面无关)。

- SessionStore:对话历史以 JSON 落盘 / 加载 / 列出,供 /save /load /sessions 命令
- export_markdown:把消息历史整理为可读的 Markdown 文档,供 /export md 使用

历史消息即 Agent._messages 的结构(dict 列表,含 user/assistant/tool 等角色),
JSON 原样保存可完整恢复;加载时由 Agent.restore_history 处理 system 消息。
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

# 会话默认存储目录(用户级,避免依赖启动时的工作目录)
DEFAULT_SESSION_DIR = Path.home() / ".xagent" / "sessions"

# 会话名白名单:中英文/数字/下划线/连字符,防止路径穿越与非法文件名
_NAME_PATTERN = re.compile(r"^[\w\-]{1,64}$", re.UNICODE)

# 合法的消息角色
_ROLES = {"system", "user", "assistant", "tool"}


class SessionError(Exception):
    """会话文件操作失败(不存在/损坏/非法名称)。"""


def _default_name() -> str:
    """未指定会话名时按时间生成,如 session-20261028-153045。"""
    return "session-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def _validate_messages(messages: list) -> None:
    """保存前校验消息结构,尽早发现异常数据。"""
    if not isinstance(messages, list):
        raise SessionError("会话数据必须是消息列表")
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") not in _ROLES:
            raise SessionError(f"第 {i} 条消息结构非法(缺少合法的 role 字段)")


class SessionStore:
    """对话历史的文件存储。"""

    def __init__(self, directory: Path = DEFAULT_SESSION_DIR) -> None:
        self._dir = directory

    def _path_of(self, name: str, suffix: str = ".json") -> Path:
        if not _NAME_PATTERN.match(name):
            raise SessionError(
                f"非法会话名「{name}」:仅允许中英文/数字/下划线/连字符,最长 64 字符"
            )
        return self._dir / f"{name}{suffix}"

    # ===== 保存与加载 =====

    def save(self, messages: list, name: str | None = None) -> Path:
        """保存会话;返回落盘路径。name 为空时按时间自动命名。

        采用「临时文件 + 原子替换」写入:先写同目录的 .tmp,成功后再
        os.replace 覆盖目标。中途失败(磁盘满/进程中断)只留下 .tmp 并被清理,
        不会出现半截 JSON 导致会话不可用。
        """
        _validate_messages(messages)
        path = self._path_of(name or _default_name())
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp_path, path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SessionError(f"会话保存失败: {exc}") from exc
        return path

    def load(self, name: str) -> list:
        """加载会话,返回消息列表;文件损坏时抛 SessionError。"""
        path = self._path_of(name)
        if not path.is_file():
            raise SessionError(f"会话「{name}」不存在(可用 /sessions 查看列表)")
        try:
            messages = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"会话「{name}」读取失败或文件损坏: {exc}") from exc
        _validate_messages(messages)
        return messages

    def list_sessions(self) -> list[tuple[str, str]]:
        """列出全部会话,返回 (名称, 保存时间) 按时间倒序。"""
        if not self._dir.is_dir():
            return []
        items = []
        for path in self._dir.glob("*.json"):
            try:
                mtime = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)
                )
            except OSError:
                continue
            items.append((path.stem, mtime))
        return sorted(items, key=lambda item: item[1], reverse=True)

    # ===== 导出 =====

    def export_markdown(self, messages: list, name: str | None = None) -> Path:
        """导出当前对话为 Markdown 文件,返回落盘路径。"""
        path = self._path_of(name or ("export-" + _default_name()), ".md")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                export_markdown(messages), encoding="utf-8"
            )
        except OSError as exc:
            raise SessionError(f"Markdown 导出失败: {exc}") from exc
        return path


def export_markdown(messages: list) -> str:
    """把消息历史整理为 Markdown 文档(system 消息不导出)。

    工具调用/结果渲染为引用块,内容超长时截断,避免导出文件膨胀。
    """
    lines = [
        "# xAgent 对话导出",
        "",
        f"- 导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for msg in messages:
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if role == "user":
            lines += [f"## 用户", "", content, ""]
        elif role == "assistant":
            lines += ["## 助手", ""]
            if content:
                lines += [content, ""]
            for call in msg.get("tool_calls") or []:
                func = call.get("function", {})
                lines.append(f"> 调用工具: {func.get('name')} 参数: {func.get('arguments')}")
            if msg.get("tool_calls"):
                lines.append("")
        elif role == "tool":
            preview = content if len(content) <= 500 else content[:500] + "…(已截断)"
            lines += [f"> [工具结果] {preview}", ""]
        # system 消息不导出
    return "\n".join(lines)
