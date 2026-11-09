"""recallContext 工具:检索 /compact 压缩前的原始上下文归档。

配合上下文压缩使用:压缩后历史只剩摘要,LLM 发现缺少历史细节时,
通过本工具用关键词检索 .agents/context/ 下的 JSONL 归档,取回原文片段。
检索后端在 core.compaction 中实现(纯本地全文关键词匹配,无向量库)。
"""

from pathlib import Path
from typing import Any

from core.compaction import search_archives

from .registry import ToolSpec, text_result_limit


def build_context_tools(context_dir: Path) -> list[ToolSpec]:
    """构建上下文检索工具集;context_dir 为归档目录(.agents/context/)。"""

    def recall_context(arguments: dict[str, Any]) -> str:
        text = str(arguments.get("text") or "").strip()
        if not text:
            return "参数错误:缺少必填的 text(要查找的关键词或文本)。"
        archive_id = arguments.get("id")
        result = search_archives(
            text,
            context_dir,
            archive_id=str(archive_id).strip() if archive_id else None,
        )
        # 双保险截断:search_archives 已按条数/片段长度约束,
        # 这里再按工具层统一约定兜底,保证不占满上下文
        limit = text_result_limit()
        if len(result) > limit:
            result = (
                result[:limit]
                + "\n…(检索结果过长已截断,可换更精确的关键词或指定归档编号重试)"
            )
        return result

    return [
        ToolSpec(
            name="recallContext",
            description=(
                "检索历史上下文归档(此前 /compact 压缩前的完整对话记录)。"
                "当上下文摘要中缺少需要的历史细节时,用关键词查找原文片段;"
                "返回命中内容、所在归档编号与位置。"
                "参数 id 为归档编号(如 20261022-153000),省略时在全部归档中检索(最新优先)。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "归档编号,如 20261022-153000;省略时检索全部归档。",
                    },
                    "text": {
                        "type": "string",
                        "description": "要查找的关键词或文本(必填)。",
                    },
                },
                "required": ["text"],
            },
            handler=recall_context,
        )
    ]
