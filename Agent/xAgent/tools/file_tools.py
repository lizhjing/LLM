"""本地文件操作工具。

全部使用 Python 标准库(pathlib / os / shutil)实现,跨平台一致,
不依赖 shell 命令,避免转义问题。
"""

import os
import shutil
from pathlib import Path
from typing import Any

from .registry import ToolRegistry, ToolSpec, text_result_limit

# 纯文本扩展名白名单,用于 readFile / writeFile 校验
_TEXT_EXTS = {
    ".txt", ".py", ".json", ".md", ".csv", ".html", ".htm",
    ".css", ".js", ".ini", ".yml", ".yaml", ".log", ".cfg",
    ".xml", ".rtf", ".properties", ".toml",
}


def get_cwd(_: dict[str, Any]) -> str:
    return os.getcwd()


def list_dir(arguments: dict[str, Any]) -> str:
    path = arguments["path"]
    target = Path(path)
    if not target.exists():
        return f"路径不存在: {path}"
    if not target.is_dir():
        return f"不是目录: {path}"
    names = sorted(target.iterdir())
    lines = [item.name + ("/" if item.is_dir() else "") for item in names]
    return "\n".join(lines) if lines else "(空目录)"


def get_file_type(arguments: dict[str, Any]) -> str:
    path = arguments["path"]
    target = Path(path)
    if not target.exists():
        return f"路径不存在: {path}"
    if target.is_dir():
        return f"目录(文件夹): {target}"
    ext = target.suffix.lower() or "(无扩展名)"
    size = target.stat().st_size
    return f"文件: {target.name}  扩展名: {ext}  大小: {size} 字节"


def read_file(arguments: dict[str, Any]) -> str:
    path = arguments["path"]
    target = Path(path)
    if not target.exists():
        return f"文件不存在: {path}"
    if target.is_dir():
        return f"路径是目录而非文件: {path}"
    if target.suffix.lower() not in _TEXT_EXTS:
        return f"不支持读取该文件类型({target.suffix}),仅支持纯文本文件"
    # 单次读取的最大字符数,避免超长内容撑爆对话上下文(上限来自 [tools] 配置)
    limit = text_result_limit()
    try:
        # 增量读取:最多读上限+1 字符,超大文件不会整文件进内存
        with target.open("r", encoding="utf-8") as fh:
            content = fh.read(limit + 1)
    except UnicodeDecodeError:
        return "文件编码非 UTF-8,无法以纯文本方式读取"
    if len(content) > limit:
        content = content[:limit] + f"\n...(内容已截断,超过{limit}字符)"
    return content


def _allowed_roots(env_name: str) -> list[Path]:
    """返回启动工作区与指定环境变量追加的根目录(分号分隔)。"""
    roots = [Path.cwd().resolve()]
    extra = os.environ.get(env_name, "")
    roots.extend(Path(p.strip()).resolve() for p in extra.split(";") if p.strip())
    return roots


def _delete_roots() -> list[Path]:
    """删除类操作允许的根目录:启动工作区 + 环境变量追加项(分号分隔)。"""
    return _allowed_roots("XAGENT_DELETE_ROOTS")


def _write_blocked(path: Path) -> str | None:
    """返回拒绝写入或改名的原因;None 表示路径位于允许的工作区内。"""
    resolved = path.resolve()
    roots = _allowed_roots("XAGENT_WRITE_ROOTS")
    if any(resolved == root or root in resolved.parents for root in roots):
        return None
    return (
        "安全限制:仅允许写入或改名工作区内的路径("
        + "; ".join(str(r) for r in roots)
        + ")。如需其他位置,请设置环境变量 XAGENT_WRITE_ROOTS(分号分隔)"
    )


def _delete_blocked(path: Path) -> str | None:
    """返回拒绝删除的原因;None 表示允许。系统目录不在白名单内,天然被拒。"""
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        return "安全限制:不允许删除盘符根目录"
    roots = _delete_roots()
    if any(resolved == root or root in resolved.parents for root in roots):
        return None
    return (
        "安全限制:仅允许删除工作区内的路径("
        + "; ".join(str(r) for r in roots)
        + ")。如需其他位置,请设置环境变量 XAGENT_DELETE_ROOTS(分号分隔)"
    )


def create_file(arguments: dict[str, Any]) -> str:
    dir_path = arguments["dir"]
    filename = arguments["filename"]
    content = arguments.get("content", "")
    dir_target = Path(dir_path)
    if not dir_target.exists():
        return f"目录不存在: {dir_path}"
    if not dir_target.is_dir():
        return f"路径不是目录: {dir_path}"
    if Path(filename).name != filename or ":" in filename or not filename.strip("."):
        return (
            "非法文件名: filename 只能是纯文件名,不能包含路径分隔符、"
            "盘符或全为点号(防目录逃逸)"
        )
    file_path = dir_target / filename
    try:
        file_path.resolve().relative_to(dir_target.resolve())
    except ValueError:
        return f"安全限制:解析后的路径越出了目标目录: {filename}"
    if file_path.exists():
        return f"文件已存在: {file_path}"
    file_path.write_text(content, encoding="utf-8")
    return f"已创建文件: {file_path}"


def write_file(arguments: dict[str, Any]) -> str:
    path = arguments["path"]
    content = arguments["content"]
    target = Path(path)
    blocked = _write_blocked(target)
    if blocked:
        return blocked
    if not target.exists():
        return f"文件不存在: {path}"
    if target.is_dir():
        return f"路径是目录而非文件: {path}"
    if target.suffix.lower() not in _TEXT_EXTS:
        return f"不支持编辑该文件类型({target.suffix}),仅支持纯文本文件"
    target.write_text(content, encoding="utf-8")
    return f"已写入文件: {path}({len(content)} 字符)"


def create_dir(arguments: dict[str, Any]) -> str:
    path = arguments["path"]
    target = Path(path)
    if target.exists():
        return f"路径已存在: {path}"
    target.mkdir(parents=True, exist_ok=True)
    return f"已创建目录: {path}"


def remove_dir(arguments: dict[str, Any]) -> str:
    path = arguments["path"]
    target = Path(path)
    if not target.exists():
        return f"目录不存在: {path}"
    if not target.is_dir():
        return f"路径不是目录: {path}"
    blocked = _delete_blocked(target)
    if blocked:
        return blocked
    shutil.rmtree(target)
    return f"已删除目录及其全部内容: {path}"


def remove_file(arguments: dict[str, Any]) -> str:
    path = arguments["path"]
    target = Path(path)
    if not target.exists():
        return f"文件不存在: {path}"
    if target.is_dir():
        return f"路径是目录而非文件,请用 removeDir: {path}"
    blocked = _delete_blocked(target)
    if blocked:
        return blocked
    target.unlink()
    return f"已删除文件: {path}"


def rename_path(arguments: dict[str, Any]) -> str:
    old = arguments["old_path"]
    new = arguments["new_path"]
    old_target = Path(old)
    new_target = Path(new)
    blocked = _write_blocked(old_target) or _write_blocked(new_target)
    if blocked:
        return blocked
    if not old_target.exists():
        return f"原路径不存在: {old}"
    if new_target.exists():
        return f"新路径已存在: {new}"
    old_target.rename(new_target)
    return f"已重命名: {old} → {new}"


def register(registry: ToolRegistry) -> None:
    """注册全部文件操作工具,参数名与原 chat.py 保持一致。"""
    definitions = [
        ToolSpec(
            name="getCwd",
            description="获取当前工作目录(脚本运行时的工作路径)",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=get_cwd,
        ),
        ToolSpec(
            name="listDir",
            description="列出指定目录下的所有文件和子目录名称。目录名后加 / 以示区分。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录路径,如 e:\\LLM 或 .",
                    }
                },
                "required": ["path"],
            },
            handler=list_dir,
        ),
        ToolSpec(
            name="getFileType",
            description="获取指定路径的类型(文件/目录/不存在),文件额外返回扩展名和大小。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录的路径"}
                },
                "required": ["path"],
            },
            handler=get_file_type,
        ),
        ToolSpec(
            name="readFile",
            description="读取指定纯文本文件的内容。仅支持 txt/py/json/md/csv/html/css/js/ini/yml/yaml/log/cfg/xml 等纯文本格式,不支持二进制文件(如 exe、jpg、pdf)。内容超过2000字符会截断。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要读取的文件路径"}
                },
                "required": ["path"],
            },
            handler=read_file,
        ),
        ToolSpec(
            name="createFile",
            description="在指定目录下创建一个新的纯文本文件。新建文件用本工具;修改已有文件用 writeFile。注意 .docx/.xlsx 等二进制文档不能用本工具,应改用 officecli(RunCli)。",
            parameters={
                "type": "object",
                "properties": {
                    "dir": {
                        "type": "string",
                        "description": "目标目录路径,如 e:\\LLM\\Agent",
                    },
                    "filename": {
                        "type": "string",
                        "description": "要创建的文件名,如 hello.txt",
                    },
                    "content": {
                        "type": "string",
                        "description": "文件初始内容,可为空字符串",
                    },
                },
                "required": ["dir", "filename", "content"],
            },
            handler=create_file,
        ),
        ToolSpec(
            name="writeFile",
            description="覆盖写入指定的纯文本文件。仅支持纯文本格式;文件必须已存在,不存在会报错。新建文件请用 createFile。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要编辑的文件路径"},
                    "content": {
                        "type": "string",
                        "description": "要写入的新内容,将覆盖原文件",
                    },
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        ),
        ToolSpec(
            name="createDir",
            description="创建一个新目录,若父目录不存在会一并创建(等价于 mkdir -p)。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要创建的目录路径"}
                },
                "required": ["path"],
            },
            handler=create_dir,
        ),
        ToolSpec(
            name="removeDir",
            description="删除指定目录及其下所有内容(包括子目录和文件)。危险操作,请谨慎使用。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的目录路径"}
                },
                "required": ["path"],
            },
            handler=remove_dir,
        ),
        ToolSpec(
            name="removeFile",
            description="删除指定文件。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件路径"}
                },
                "required": ["path"],
            },
            handler=remove_file,
        ),
        ToolSpec(
            name="renamePath",
            description="重命名文件或目录(仅改名称,不能跨盘符移动)。",
            parameters={
                "type": "object",
                "properties": {
                    "old_path": {
                        "type": "string",
                        "description": "原路径(文件或目录)",
                    },
                    "new_path": {
                        "type": "string",
                        "description": "新路径(仅改名称,不能跨盘符)",
                    },
                },
                "required": ["old_path", "new_path"],
            },
            handler=rename_path,
        ),
    ]
    for spec in definitions:
        registry.register(spec)
