"""Skill 工具:loadSkill / createSkill。

- loadSkill:启动时 Agent 仅在 system 提示中携带各 Skill 的 name 与 description;
  LLM 判断任务匹配某个 Skill 时,调用本工具读取其 SKILL.md 正文并遵循执行。
- createSkill:用户要求创建新技能时,由 LLM 调用本工具在项目工作区的
  .agents/skills/ 下落盘新 Skill(子目录 + SKILL.md + 可选附件);
  校验、写入与即时注册由 core.skills.SkillRegistry.create_skill 实现。
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


def _make_create_handler(skill_registry: "SkillRegistry | None") -> Any:
    """构造 createSkill 处理函数:整理参数后交给 SkillRegistry 校验与落盘。"""

    def create_skill(arguments: dict[str, Any]) -> str:
        if skill_registry is None:
            return "Skill 功能未启用:当前没有可用的 Skill 注册表"
        raw_attachments = arguments.get("attachments")
        attachments: list[tuple[str, str]] = []
        if raw_attachments is not None:
            if not isinstance(raw_attachments, list):
                return "参数错误:attachments 必须是数组,每项为 {filename, content} 对象"
            for item in raw_attachments:
                if not isinstance(item, dict):
                    return "参数错误:attachments 每一项必须是含 filename 与 content 的对象"
                attachments.append(
                    (str(item.get("filename") or ""), str(item.get("content") or ""))
                )
        return skill_registry.create_skill(
            name=str(arguments.get("name") or ""),
            description=str(arguments.get("description") or ""),
            body=str(arguments.get("content") or ""),
            attachments=attachments,
        )

    return create_skill


def register(registry: ToolRegistry, skill_registry: "SkillRegistry | None" = None) -> None:
    """注册 loadSkill 与 createSkill 工具;skill_registry 由应用装配时传入(main.py)。"""
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
    registry.register(
        ToolSpec(
            name="createSkill",
            description=(
                "在项目工作区的 .agents/skills/ 下创建一个新 Skill(技能)。"
                "当用户要求创建技能、新增 Skill,或希望把某个流程/规范沉淀为可复用技能时,"
                "主动调用本工具,不要用 createFile/createDir 手工拼装。"
                "工具会以 name 命名创建英文子目录,写入带 YAML Frontmatter"
                "(name/description)的 SKILL.md,并可按需写入附件文件;"
                "创建成功后该 Skill 立即可用 loadSkill 加载。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Skill 名称,仅限小写字母/数字/连字符,最长 64 字符,"
                            "如 'weekly-report';同时作为 .agents/skills/ 下的子目录名"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "一句话描述该 Skill 的用途与适用场景,将写入 frontmatter "
                            "并展示在系统提示中,供后续任务匹配"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "SKILL.md 正文(Markdown 操作指引),不要包含 frontmatter,"
                            "工具会自动生成头部"
                        ),
                    },
                    "attachments": {
                        "type": "array",
                        "description": "可选:Skill 的附加文件(模板/示例/参考文档等)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "filename": {
                                    "type": "string",
                                    "description": "附件路径,相对于 Skill 目录,如 'assets/template.md'",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "附件的文本内容",
                                },
                            },
                            "required": ["filename", "content"],
                        },
                    },
                },
                "required": ["name", "description", "content"],
            },
            handler=_make_create_handler(skill_registry),
        )
    )
