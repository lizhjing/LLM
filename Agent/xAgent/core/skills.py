"""Agent Skills 规范的发现与加载(渐进式披露)。

规范要点:
- 项目根目录下 .agents/skills/ 的每个子目录是一个 Skill,至少包含一个 SKILL.md
- SKILL.md 开头用 YAML Frontmatter 声明 name 与 description 两个必需字段
- 初始化阶段仅加载 name 与 description(轻量);完整正文由 LLM 通过 loadSkill
  工具按需读取,避免把所有 Skill 全文塞进上下文

frontmatter 解析使用标准库实现,支持单行值与 | / > 块标量,满足常见 SKILL.md
写法;不做完整 YAML 解析以保持零依赖。
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# name 规范:小写字母/数字 + 连字符(不以其开头/结尾),最长 64 字符
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_LEN = 64
_MAX_DESC_LEN = 1024  # 规范对 description 的长度上限,超出截断以保护上下文


@dataclass(frozen=True)
class SkillMeta:
    """一个已注册 Skill 的元信息(不含正文)。"""

    name: str
    description: str
    path: Path  # SKILL.md 完整路径,供按需读取正文


def split_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    """解析文件开头的 YAML Frontmatter。

    返回 (字段字典, 正文);没有合法 frontmatter 时返回 (None, 原文)。
    简化实现:支持 `key: value`(可带引号)与 `|`/`>` 块标量,足够覆盖
    Agent Skills 规范要求的 name/description 字段。
    """
    if not text.startswith("---"):
        return None, text
    lines = text.splitlines()
    end_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end_index = i
            break
    if end_index is None:
        return None, text

    fields: dict[str, str] = {}
    i = 1
    while i < end_index:
        line = lines[i]
        # 顶层键必须顶格且含冒号;缩进行属于上一个键的块标量,已在上轮消费
        if ":" not in line or line[:1] in (" ", "\t"):
            i += 1
            continue
        key, _, raw = line.partition(":")
        key = key.strip().lower()
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]  # 去除成对引号

        if value in ("|", "|-", ">", ">-"):  # 块标量:消费后续缩进/空行
            block: list[str] = []
            i += 1
            while i < end_index and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
                block.append(lines[i].strip())
                i += 1
            value = "\n".join(block) if value.startswith("|") else " ".join(block)
        else:
            i += 1

        if key:
            fields[key] = value
    return fields, "\n".join(lines[end_index + 1:])


def _is_inside(path: Path, directory: Path) -> bool:
    """判断 path 是否位于 directory 内部(含 directory 本身)。

    使用 normcase 消除 Windows 路径大小写差异,防止绕过校验。
    """
    try:
        p = os.path.normcase(os.path.abspath(str(path)))
        d = os.path.normcase(os.path.abspath(str(directory)))
        return p == d or p.startswith(d + os.sep)
    except OSError:
        return False


class SkillRegistry:
    """Skill 注册表:启动时扫描元信息,运行期按需加载正文与附加文件。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillMeta] = {}
        # 扫描期发现的无效 Skill 及原因,由入口层在启动时提示
        self.problems: list[str] = []
        # 项目根目录,用于解析"项目相对路径"形式的附加文件;scan 时推定
        self._project_root: Path | None = None

    @classmethod
    def scan(cls, skills_dir: Path) -> "SkillRegistry":
        """扫描 skills_dir 下的一级子目录,仅加载各 SKILL.md 的元信息。

        目录不存在属于正常情况(未使用 Skill 功能),返回空注册表。
        """
        registry = cls()
        if not skills_dir.is_dir():
            return registry

        # 推定项目根目录:标准布局为 <root>/.agents/skills/
        registry._project_root = (
            skills_dir.parent.parent
            if skills_dir.parent.name == ".agents"
            else skills_dir.parent
        )

        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue  # 只认 Skill 子目录,忽略散落文件
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                registry.problems.append(f"跳过 {child.name}/: 缺少 SKILL.md")
                continue

            try:
                text = skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                registry.problems.append(f"跳过 {child.name}/: SKILL.md 读取失败({exc})")
                continue

            meta = registry._parse_meta(text, skill_md, child.name)
            if meta is None:
                continue
            if meta.name != child.name:
                registry.problems.append(
                    f"跳过 {child.name}/: name 字段 '{meta.name}' 与目录名不一致"
                )
                continue
            registry._skills[meta.name] = meta
        return registry

    # ===== 查询接口 =====

    @property
    def metas(self) -> list[SkillMeta]:
        return list(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def load_body(self, name: str) -> str:
        """按需加载 Skill 正文(SKILL.md 去掉 frontmatter 后的内容)。

        超过 2000 字符时截断并标注,与项目内容截断约定一致。
        """
        meta = self._skills.get(name)
        if meta is None:
            return self._unknown_skill_message(name)

        try:
            text = meta.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"Skill 正文读取失败: {exc}"

        _, body = split_frontmatter(text)
        body = body.strip()
        if not body:
            return f"Skill「{name}」的 SKILL.md 没有正文内容"

        if len(body) > 2000:
            body = (
                body[:2000]
                + f"\n...(正文已截断,共 {len(body)} 字符;完整内容可用 readFile 工具读取 {meta.path})"
            )
        return f"Skill「{name}」的完整内容如下:\n\n{body}"

    def load_file(self, name: str, filename: str) -> str:
        """读取 Skill 目录内的附加文件,读取范围严格限制在该 Skill 目录内部。

        路径解析优先级(先精确匹配,后搜索):
        1. 绝对路径 → 校验位于 Skill 目录内后直接读取
        2. Skill 目录相对路径精确匹配(skill_dir / filename)
        3. 项目根目录相对路径精确匹配(如 .agents/skills/<name>/xxx)
        4. 纯文件名(不含路径分隔符)→ 在 Skill 目录内递归搜索;
           唯一命中则读取,多个同名文件返回候选路径由 LLM 决定
        """
        meta = self._skills.get(name)
        if meta is None:
            return self._unknown_skill_message(name)
        raw = filename.strip()
        if not raw:
            return "参数错误:filename 不能为空,如仅加载 Skill 正文请只传 name"
        skill_dir = meta.path.parent

        def guarded(path: Path) -> str | None:
            """路径安全校验:解析后的真实路径必须在 Skill 目录内部。"""
            if not _is_inside(path, skill_dir):
                return "路径越界:文件读取范围限制在 Skill 目录内部,无法读取该路径"
            return None

        resolved: Path | None = None
        target = Path(raw)
        if target.is_absolute():
            # 1) 绝对路径
            if out_of_range := guarded(target):
                return out_of_range
            if not target.is_file():
                return f"文件不存在: {raw}"
            resolved = target
        elif (skill_dir / raw).is_file():
            # 2) Skill 目录相对路径精确匹配
            if out_of_range := guarded(skill_dir / raw):
                return out_of_range
            resolved = skill_dir / raw
        elif self._project_root is not None and (self._project_root / raw).is_file():
            # 3) 项目根目录相对路径精确匹配
            if out_of_range := guarded(self._project_root / raw):
                return out_of_range
            resolved = self._project_root / raw
        elif not any(sep in raw for sep in ("/", "\\")):
            # 4) 纯文件名:递归搜索 Skill 目录
            matches = [p for p in sorted(skill_dir.rglob(raw)) if p.is_file()]
            if not matches:
                return f"Skill「{name}」目录内未找到文件「{raw}」"
            if len(matches) > 1:
                # as_posix 统一用正斜杠展示,便于 LLM 原样回传(pathlib 两种分隔符都接受)
                listing = "\n".join(
                    f"- {m.relative_to(skill_dir).as_posix()}" for m in matches
                )
                return (
                    f"找到多个同名文件「{raw}」,请从以下候选中选择,"
                    "将 filename 传为对应相对路径后重新调用:\n" + listing
                )
            resolved = matches[0]

        if resolved is None:
            return (
                f"未找到文件「{raw}」;请检查路径,或仅提供文件名以便在 Skill 目录内搜索"
            )

        if resolved == meta.path:
            # SKILL.md 已随 name 加载,避免重复占用上下文
            return "SKILL.md 的正文已通过 name 参数加载过,无需重复读取"

        return self._read_text_file(resolved, skill_dir)

    # ===== 内部实现 =====

    def _unknown_skill_message(self, name: str) -> str:
        if not self._skills:
            return "当前没有可用的 Skill(项目根目录下未找到 .agents/skills/ 目录,或其中没有有效的 SKILL.md)"
        return f"未知 Skill: {name}(当前可用的 Skill: {', '.join(self._skills)})"

    def _read_text_file(self, path: Path, skill_dir: Path) -> str:
        """读取文本文件并整理返回;截断约定与项目一致(2000 字符)。"""
        label = str(path.relative_to(skill_dir))
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"文件 {label} 无法以文本方式读取(可能是二进制文件)"
        except OSError as exc:
            return f"文件 {label} 读取失败: {exc}"

        if not text.strip():
            return f"文件 {label} 是空文件"
        if len(text) > 2000:
            text = (
                text[:2000]
                + f"\n...(内容已截断,共 {len(text)} 字符;完整内容可用 readFile 工具读取 {path})"
            )
        return f"文件 {label} 的内容如下:\n\n{text}"

    def _parse_meta(self, text: str, path: Path, dir_name: str) -> SkillMeta | None:
        """解析并校验 frontmatter;无效时记录问题并返回 None。"""
        fields, _ = split_frontmatter(text)
        if fields is None:
            self.problems.append(f"跳过 {dir_name}/: SKILL.md 缺少 YAML Frontmatter")
            return None

        name = fields.get("name", "").strip()
        description = fields.get("description", "").strip()
        if not name:
            self.problems.append(f"跳过 {dir_name}/: 缺少必需字段 name")
            return None
        if len(name) > _MAX_NAME_LEN or not _NAME_PATTERN.match(name):
            self.problems.append(
                f"跳过 {dir_name}/: name '{name}' 不符合规范(小写字母/数字/连字符,最长 {_MAX_NAME_LEN})"
            )
            return None
        if not description:
            self.problems.append(f"跳过 {dir_name}/: 缺少必需字段 description")
            return None
        if len(description) > _MAX_DESC_LEN:
            description = description[:_MAX_DESC_LEN]
        return SkillMeta(name=name, description=description, path=path)
