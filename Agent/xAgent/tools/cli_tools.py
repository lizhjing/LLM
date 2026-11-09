"""CLI 工具:RunCli —— 让 Agent 通过 subprocess 调用本地命令行程序。

安全边界:命令必须在白名单内(默认集合,可用环境变量 XAGENT_CLI_ALLOWLIST
覆盖);shell/脚本宿主(cmd/powershell/wscript 等)无条件禁止,防止 `cmd /c`
类任意命令执行。程序名与参数始终以列表 + shell=False 执行,无拼接注入问题。
"""

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .registry import ToolRegistry, ToolSpec, text_result_limit

_DEFAULT_TIMEOUT = 30  # 默认执行超时(秒)
_MAX_TIMEOUT = 300     # 允许设置的超时上限


def _decode(data: bytes | None) -> str:
    """解码子进程输出。

    不用「UTF-8 失败再回退本地编码」策略:中文 Windows 下 GBK 字节大多恰好是
    合法 UTF-8(解码成错误汉字而不报错),该策略会失效。改用 requests 自带的
    charset_normalizer 做编码探测,并用 cp_isolation 把候选限定为 UTF-8 与
    GB18030(GBK 超集),避免 GBK 被误判为 EUC-KR 等其他 CJK 编码;探测失败时
    兜底 UTF-8 替换解码。
    """
    if not data:
        return ""
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data, cp_isolation=["utf_8", "gb18030"]).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    return data.decode("utf-8", errors="replace")


def _truncate(text: str) -> str:
    """超长输出截断并标明截断情况,避免大量终端输出占用上下文。"""
    limit = text_result_limit()
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n...(已截断:原始输出共 {len(text)} 字符,仅保留前 {limit} 字符)"
    )


# ===== 命令执行安全边界 =====
# 始终禁止的 shell/脚本宿主(即使被加入白名单也不放行),堵住 cmd /c、python -c 类任意执行
_ALWAYS_BLOCKED = {
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "wscript", "wscript.exe", "cscript", "cscript.exe", "mshta", "mshta.exe",
    "regsvr32", "regsvr32.exe", "rundll32", "rundll32.exe", "certutil", "certutil.exe",
}
# 默认命令白名单;环境变量 XAGENT_CLI_ALLOWLIST(逗号分隔)可整体覆盖
_DEFAULT_ALLOWLIST = {
    "officecli", "git", "python", "python3", "pip", "node", "npm",
    "where", "dir", "type", "findstr", "curl", "ping", "ipconfig",
}


def _allowlist() -> set[str]:
    extra = os.environ.get("XAGENT_CLI_ALLOWLIST", "")
    if extra.strip():
        return {item.strip().lower() for item in extra.split(",") if item.strip()}
    return _DEFAULT_ALLOWLIST


def _command_rejected(command: str) -> str | None:
    """返回拒绝执行的原因;None 表示允许。"""
    name = Path(command).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in _ALWAYS_BLOCKED:
        return f"安全限制:禁止执行 shell/脚本宿主「{command}」(防止任意命令执行)"
    if name not in _allowlist():
        allowed = ", ".join(sorted(_allowlist()))
        return (
            f"安全限制:命令「{command}」不在白名单内。允许的命令: {allowed};"
            "如需扩展,请设置环境变量 XAGENT_CLI_ALLOWLIST(逗号分隔)"
        )
    return None


def run_cli(arguments: dict[str, Any]) -> str:
    """执行本地命令并整理 stdout / stderr / 退出码;所有异常均转为可读结果。"""
    command = str(arguments.get("command", "")).strip()
    if not command:
        return "参数错误:command 不能为空"
    rejected = _command_rejected(command)
    if rejected:
        return rejected

    # args 应为字符串列表;若模型误传字符串则按空白切分做兼容
    raw_args = arguments.get("args") or []
    if isinstance(raw_args, str):
        raw_args = shlex.split(raw_args)
    args = [str(item) for item in raw_args]

    # 可选超时参数,非法输入回退默认值,并限制在 1~300 秒
    timeout = _DEFAULT_TIMEOUT
    if arguments.get("timeout") is not None:
        try:
            timeout = int(arguments["timeout"])
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, _MAX_TIMEOUT))

    try:
        completed = subprocess.run(
            [command, *args],   # 参数列表方式,等价于 shell=False(显式写出以示意图)
            capture_output=True,  # 分别捕获 stdout 与 stderr
            shell=False,          # 不经过 shell,无命令拼接/注入问题
            timeout=timeout,
        )
    except FileNotFoundError:
        return f"执行失败:未找到命令「{command}」,请确认程序已安装并加入 PATH"
    except PermissionError:
        return f"执行失败:没有权限执行「{command}」"
    except subprocess.TimeoutExpired as exc:
        # 超时:进程已被强制终止,附带已产生的部分输出供 LLM 判断
        message = f"执行超时:命令超过 {timeout} 秒已被强制终止(退出码: None)"
        partial_out = _truncate(_decode(exc.stdout))
        partial_err = _truncate(_decode(exc.stderr))
        if partial_out:
            message += f"\nstdout(已产生的部分输出):\n{partial_out}"
        if partial_err:
            message += f"\nstderr(已产生的部分输出):\n{partial_err}"
        return message

    stdout = _truncate(_decode(completed.stdout))
    stderr = _truncate(_decode(completed.stderr))
    return (
        f"退出码: {completed.returncode}\n"
        f"stdout:\n{stdout if stdout else '(空)'}\n"
        f"stderr:\n{stderr if stderr else '(空)'}"
    )


def register(registry: ToolRegistry) -> None:
    """注册 RunCli 工具。"""
    registry.register(
        ToolSpec(
            name="RunCli",
            description=(
                "执行本地命令行程序并返回 stdout、stderr 与退出码,供判断执行结果并决定下一步。"
                "以参数列表方式执行(shell=False),在当前工作目录以当前用户权限运行,"
                "仅用于受控教学实验环境。示例:command='git', args=['--version']。"
                "操作 Office 文档(.docx/.xlsx/.pptx)前,务必先用 loadSkill 加载 officecli "
                "技能获取正确命令语法;不要反复运行 --help,其输出长且会被截断。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "程序名称或完整路径,如 python、git、ipconfig",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参数列表,如 ['--version'] 或 ['-c', 'print(1)'];可选",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "执行超时秒数,1~300,默认 30;可选",
                    },
                },
                "required": ["command"],
            },
            handler=run_cli,
        )
    )
