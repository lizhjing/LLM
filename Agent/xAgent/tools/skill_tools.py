"""Skill 工具:loadSkill —— 让 LLM 按需加载 Skill 的完整正文。

启动时 Agent 仅在 system 提示中携带各 Skill 的 name 与 description;
LLM 判断任务匹配某个 Skill 时,调用本工具读取其 SKILL.md 正文并遵循执行。
"""

from typing import TYPE_CHECKING, Any

from .registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:  # 仅类型检查引用,避免 tools -> core 的运行时反向依赖
    from core.skills import SkillRegistry


def _make_load_handler(skill_registry: "SkillRegistry | None") -> Any:
    """构造 loadSkill 处理函数;注册表在装配时绑定,缺失时返回未启用提示。"""

    def load_skill(arguments: dict[str, Any]) -> str:
        if skill_registry is None:
            return "Skill 功能未启用:当前项目没有可用的 Skill 注册表"
        name = str(arguments.get("name", "")).strip()
        if not name:
            return "参数错误:name 不能为空,请提供要加载的 Skill 名称"
        # filename 为可选参数:提供时读取附加文件,否则读取 SKILL.md 正文
        filename = str(arguments.get("filename") or "").strip()
        if filename:
            return skill_registry.load_file(name, filename)
        return skill_registry.load_body(name)

    return load_skill


def register(registry: ToolRegistry, skill_registry: "SkillRegistry | None" = None) -> None:
    """注册 loadSkill 工具;skill_registry 由应用装配时传入(main.py)。"""
    registry.register(
        ToolSpec(
            name="loadSkill",
            description=(
                "加载 Skill 的内容(Skill 是预置的任务流程/规范文档)。"
                "当用户任务与 system 提示中列出的某个 Skill 描述匹配时,先调用本工具"
                "获取其正文,再按正文指引完成任务。"
                "仅传 name 时返回该 Skill 的 SKILL.md 全文;"
                "同时传 filename 时读取该 Skill 的附加文件(支持 Skill 目录相对路径、"
                "项目相对路径或绝对路径;仅提供文件名时在 Skill 目录内递归搜索,"
                "多个同名文件会返回候选列表),读取范围限制在该 Skill 目录内部。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill 名称,必须是 system 提示中列出的 name",
                    },
                    "filename": {
                        "type": "string",
                        "description": (
                            "可选:要加载的附加文件路径或名称;"
                            "如 'assets/template.md'、'.agents/skills/demo-report/assets/template.md' "
                            "或仅文件名 'template.md'"
                        ),
                    },
                },
                "required": ["name"],
            },
            handler=_make_load_handler(skill_registry),
        )
    )
