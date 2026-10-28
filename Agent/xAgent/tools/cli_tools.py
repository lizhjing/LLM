"""CLI 工具:RunCli —— 让 Agent 通过 subprocess 调用本地命令行程序。

仅用于受控教学实验环境:不设命令白名单/黑名单/审批机制,命令以运行 xAgent
的当前用户权限执行。始终把程序名与参数拼装为列表并以 shell=False 执行,
避免拼接 Shell 命令字符串带来的转义与注入问题。
"""

import shlex
import subprocess
from typing import Any

from .registry import ToolRegistry, ToolSpec

_DEFAULT_TIMEOUT = 30  # 默认执行超时(秒)
_MAX_TIMEOUT = 300     # 允许设置的超时上限
# 单个流(stdout/stderr)的最大返回字符数,与项目 2000 字符截断约定一致
_MAX_OUTPUT_CHARS = 2000


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
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return (
        text[:_MAX_OUTPUT_CHARS]
        + f"\n...(已截断:原始输出共 {len(text)} 字符,仅保留前 {_MAX_OUTPUT_CHARS} 字符)"
    )


def run_cli(arguments: dict[str, Any]) -> str:
    """执行本地命令并整理 stdout / stderr / 退出码;所有异常均转为可读结果。"""
    command = str(arguments.get("command", "")).strip()
    if not command:
        return "参数错误:command 不能为空"

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
