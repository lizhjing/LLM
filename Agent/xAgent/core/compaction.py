"""上下文压缩与历史归档:/compact 的核心实现与 recallContext 的检索后端。

设计约束(简单可理解,不引入向量库 / Embedding):
- 归档:压缩前把完整消息序列逐行写成 JSONL,存放在项目 .agents/context/ 下,
  文件名即归档编号(YYYYMMDD-HHMMSS),保留角色、内容、工具调用、工具结果与顺序
- 压缩:调用 LLM(不带工具定义)生成约 5,000 Tokens 的 Markdown 摘要,
  用「system + 摘要」替换原历史;原始内容只存在于归档文件中
- 检索:recallContext 对归档做大小写不敏感的关键词全文匹配,返回命中片段
  + 归档编号 + 行位置,并限制结果总量,避免检索结果再次占满上下文
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# 模型上下文窗口未知时的默认值:128k(128 * 1024 tokens)
DEFAULT_CONTEXT_WINDOW = 131072

# Token 估算系数:中英混合的 JSON 对话内容按 3 字符 ≈ 1 Token 近似(偏保守)。
# 仅在 API 未返回 usage 时兜底使用;能拿到 API 统计时始终以 API 为准。
_CHARS_PER_TOKEN = 3

# 检索结果约束:最多返回条数与单条片段长度,防止结果占满上下文
_MAX_SEARCH_RESULTS = 6
_SNIPPET_LEN = 240


def estimate_tokens(
    messages: list[dict[str, Any]], chars_per_token: int = _CHARS_PER_TOKEN
) -> int:
    """估算一组消息的 Token 数,含 system、对话历史与工具调用/结果消息。

    每条消息计:角色名 + content 文本 + tool_calls 的 JSON 序列化 + 固定结构开销;
    结果向上取整,宁高勿低(提前触发压缩警告比事后溢出安全)。
    chars_per_token 可由 [agent] chars_per_token 配置覆盖。
    """
    chars = 0
    for msg in messages:
        chars += len(str(msg.get("role") or "")) + 8  # 角色 + 消息结构开销
        chars += len(str(msg.get("content") or ""))
        for call in msg.get("tool_calls") or []:
            chars += len(json.dumps(call, ensure_ascii=False))
    return -(-chars // max(1, chars_per_token))  # 向上取整


# ===== 压缩任务提示词(逐字用于 /compact 的 LLM 请求) =====

COMPACT_PROMPT = """你是一名 AI Agent 的上下文管理助手。当前会话即将达到上下文窗口限制,需要将历史对话压缩为一份高质量的工作摘要,以便 Agent 在新的上下文中继续执行任务。

请认真阅读提供的历史上下文,提取对后续工作仍然有价值的信息,并生成一份约 5,000 Tokens 的 Markdown 摘要。

摘要应重点保留以下内容:

1. **用户目标与要求**:用户希望完成什么任务?有哪些必须遵守的约束、偏好和验收条件?
2. **已经完成的工作**:哪些任务已经完成?实现了哪些功能?获得了哪些已经验证的结果?
3. **当前工作状态**:Agent 正在处理什么任务?当前进展到哪一步?是否存在尚未解决的问题?
4. **关键技术信息**:保留重要的文件路径、函数名称、接口定义、配置项、执行命令、错误信息和必要的代码片段。
5. **关键决策与原因**:记录用户和 Agent 已经确定的技术方案,以及重要的设计取舍。
6. **下一步任务**:明确接下来应该执行的操作,以及需要继续验证的事项。

请遵守以下要求:

- 优先保留对后续任务有直接影响的信息,删除寒暄、重复讨论、过时的尝试和无关内容。
- 对已经验证的事实与尚未验证的推测作出明确区分。
- 不要将计划完成的任务描述为已经完成。
- 重要的文件路径、函数名称、配置名称和命令应保留准确原文。
- 对失败的尝试,只保留有助于避免重复错误的信息。
- 如果信息存在冲突,应优先保留用户最新确认的要求,并简要说明仍然存在的冲突。
- 如果某些历史细节无法在摘要中完整保留,可简要说明它们已保存到原始上下文归档中,后续可以通过 `recallContext` 工具重新检索。
- 使用清晰的 Markdown 结构输出摘要,不要附加解释或无关内容。

最终目标:使一个从未阅读过原始对话的 Agent,仅凭这份摘要,就能够理解当前任务、继承关键决策,并继续开展工作。"""


def build_compact_request(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """构建压缩请求的消息序列:压缩提示词 + 完整历史转录。

    仅一条 user 消息,不带 system 与工具定义——压缩期间 LLM 只做摘要,
    不执行任何 Function Calling。历史以 JSON Lines 形式内嵌,与归档
    文件格式一致,便于对照。
    """
    transcript = "\n".join(json.dumps(msg, ensure_ascii=False) for msg in messages)
    content = (
        f"{COMPACT_PROMPT}\n\n"
        "===== 历史上下文(JSON Lines,每行一条消息)=====\n"
        f"{transcript}\n"
        "===== 历史上下文结束 ====="
    )
    return [{"role": "user", "content": content}]


# ===== 归档 =====

def archive_messages(
    messages: list[dict[str, Any]], context_dir: Path
) -> tuple[str, str]:
    """把消息序列完整归档为 JSONL,返回 (归档编号, 可读时间戳)。

    归档编号即文件名(不含扩展名),格式 YYYYMMDD-HHMMSS,如 20261022-153000。
    先写临时文件再原子替换,保证目录中出现的都是完整归档——这是 /compact
    流程的第一步,写入成功(不抛异常)后才允许继续执行压缩。
    """
    now = datetime.now()
    base_id = now.strftime("%Y%m%d-%H%M%S")
    context_dir.mkdir(parents=True, exist_ok=True)
    path = context_dir / f"{base_id}.jsonl"
    suffix = 0
    while path.exists():  # 同一秒内多次压缩:编号追加序号,保证唯一不覆盖
        suffix += 1
        path = context_dir / f"{base_id}-{suffix}.jsonl"
    archive_id = path.stem
    tmp = path.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)  # 归档失败时清理半成品
    return archive_id, now.strftime("%Y-%m-%d %H:%M:%S")


# ===== 检索(recallContext 后端) =====

def _message_text(msg: dict[str, Any]) -> str:
    """提取一条消息中所有可检索文本:content + 工具调用名与参数。"""
    parts = [str(msg.get("content") or "")]
    for call in msg.get("tool_calls") or []:
        func = call.get("function") or {}
        parts.append(str(func.get("name") or ""))
        parts.append(str(func.get("arguments") or ""))
    return "\n".join(p for p in parts if p)


def _snippet(text: str, pos: int, match_len: int, limit: int) -> str:
    """截取命中位置附近的片段;折叠空白为单行,首尾补省略号。"""
    start = max(0, pos - limit // 4)
    end = min(len(text), start + limit)
    piece = " ".join(text[start:end].split())
    if start > 0:
        piece = "…" + piece
    if end < len(text):
        piece = piece + "…"
    return piece


def search_archives(
    text: str,
    context_dir: Path,
    archive_id: str | None = None,
    max_results: int = _MAX_SEARCH_RESULTS,
    snippet_len: int = _SNIPPET_LEN,
) -> str:
    """在归档中做大小写不敏感的关键词全文检索,返回面向 LLM 的文本结果。

    - 指定 archive_id 时只检索该归档(接受带或不带 .jsonl 后缀的编号)
    - 未指定时检索目录下全部归档,按编号倒序(最新优先)
    - 返回命中片段、归档编号与行位置;命中过多时只展示前 max_results 条
      并注明总数,控制结果长度
    """
    keyword = text.strip()
    if not keyword:
        return "检索失败:关键词 text 不能为空。"

    if archive_id:
        name = str(archive_id).strip()
        if name.lower().endswith(".jsonl"):
            name = name[:-len(".jsonl")]
        targets = [context_dir / f"{name}.jsonl"]
        if not targets[0].is_file():
            return f"未找到编号为 {name} 的归档文件。"
    else:
        if not context_dir.is_dir():
            return "尚无任何历史归档(尚未执行过 /compact)。"
        targets = sorted(context_dir.glob("*.jsonl"), reverse=True)
        if not targets:
            return "尚无任何历史归档(尚未执行过 /compact)。"

    needle = keyword.lower()
    results: list[str] = []
    total_hits = 0

    for path in targets:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            results.append(f"[归档 {path.stem}] 读取失败: {exc}")
            continue

        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 半行损坏不影响其余行
            original = _message_text(msg)
            pos = original.lower().find(needle)
            if pos < 0:
                continue
            total_hits += 1
            if len(results) < max_results:
                role = msg.get("role") or "?"
                snippet = _snippet(original, pos, len(needle), snippet_len)
                results.append(f"[归档 {path.stem}] 第 {lineno} 条({role}):\n{snippet}")

    if not results and total_hits == 0:
        return f"在 {len(targets)} 个归档中未找到「{keyword}」。可尝试更换关键词,或省略 id 检索全部归档。"

    shown = len(results)
    header = f"检索「{keyword}」:共命中 {total_hits} 处"
    if total_hits > shown:
        header += f"(最新归档优先,仅展示前 {shown} 处;可指定归档编号或换更精确的关键词缩小范围)"
    return header + "\n\n" + "\n\n".join(results)
