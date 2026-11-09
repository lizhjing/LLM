"""Agent Skills 规范的发现与加载(渐进式披露)。

规范要点:
- 项目根目录下 .agents/skills/ 的每个子目录是一个 Skill,至少包含一个 SKILL.md
- SKILL.md 开头用 YAML Frontmatter 声明 name 与 description 两个必需字段
- 初始化阶段仅加载 name 与 description(轻量);完整正文由 LLM 通过 loadSkill
  工具按需读取,避免把所有 Skill 全文塞进上下文

frontmatter 解析使用标准库实现,支持单行值与 | / > 块标量,满足常见 SKILL.md
写法;不做完整 YAML 解析以保持零依赖。
"""

import json
import os
import re
import shutil
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


def name_problem(name: str) -> str | None:
    """校验 Skill name 是否符合规范;不合规时返回原因,合规返回 None。"""
    if not name:
        return "缺少必需字段 name"
    if len(name) > _MAX_NAME_LEN or not _NAME_PATTERN.match(name):
        return f"name '{name}' 不符合规范(小写字母/数字/连字符,最长 {_MAX_NAME_LEN})"
    return None


def _attachment_problem(filename: str, index: int) -> str | None:
    """校验 Skill 附件的相对路径;非法时返回错误说明。

    仅允许 Skill 目录内的相对路径(可含子目录),拒绝绝对路径、
    盘符/根符号、`..` 回溯与空路径,防止越界写入。
    """
    if not filename:
        return f"参数错误:第 {index} 个附件缺少 filename"
    candidate = Path(filename)
    if (
        candidate.is_absolute()
        or candidate.drive
        or candidate.root
        or not candidate.parts
        or ".." in candidate.parts
        or candidate.name in (".", "..")
    ):
        return (
            f"参数错误:第 {index} 个附件路径不合法('{filename}'),"
            "只能是 Skill 目录内的相对路径,如 'assets/template.md'"
        )
    return None


class SkillRegistry:
    """Skill 注册表:启动时扫描元信息,运行期按需加载正文与附加文件。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillMeta] = {}
        self._version = 0  # 注册表变化计数,Agent 据此重建 system 消息
        # 扫描期发现的无效 Skill 及原因,由入口层在启动时提示
        self.problems: list[str] = []
        # 项目根目录,用于解析"项目相对路径"形式的附加文件;scan 时推定
        self._project_root: Path | None = None
        # Skill 根目录列表(.agents/skills),scan 时记录;首个为 createSkill 落盘目标
        self._skills_dirs: tuple[Path, ...] = ()

    @property
    def version(self) -> int:
        """注册表版本号:每次 rescan(含 createSkill 落盘)自增。"""
        return self._version

    @property
    def _skills_dir(self) -> Path | None:
        """首个 Skill 根目录(createSkill 的落盘目标)。"""
        return self._skills_dirs[0] if self._skills_dirs else None

    @classmethod
    def scan(cls, *skills_dirs: Path) -> "SkillRegistry":
        """扫描各 skills_dir 下的一级子目录,合并为一个注册表。

        多目录时先到先得:同名 Skill 以靠前目录为准(启动工作目录可覆盖
        程序目录的内置 Skill);首个目录同时是 createSkill 的落盘目标。
        目录不存在属于正常情况(未使用 Skill 功能),静默跳过。
        """
        registry = cls()
        registry._skills_dirs = tuple(Path(d) for d in skills_dirs)
        for skills_dir in registry._skills_dirs:
            registry._scan_one(skills_dir)
        return registry

    def _scan_one(self, skills_dir: Path) -> None:
        """扫描单个 Skill 目录并合并进当前注册表(已有同名条目不覆盖)。"""
        if not skills_dir.is_dir():
            return

        # 推定项目根目录:标准布局为 <root>/.agents/skills/(取首个扫描目录)
        if self._project_root is None:
            self._project_root = (
                skills_dir.parent.parent
                if skills_dir.parent.name == ".agents"
                else skills_dir.parent
            )

        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue  # 只认 Skill 子目录,忽略散落文件
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                self.problems.append(f"跳过 {child.name}/: 缺少 SKILL.md")
                continue

            try:
                text = skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.problems.append(f"跳过 {child.name}/: SKILL.md 读取失败({exc})")
                continue

            meta = self._parse_meta(text, skill_md, child.name)
            if meta is None:
                continue
            if meta.name != child.name:
                self.problems.append(
                    f"跳过 {child.name}/: name 字段 '{meta.name}' 与目录名不一致"
                )
                continue
            if meta.name in self._skills:  # 多目录合并:先到先得
                self.problems.append(
                    f"提示: Skill '{meta.name}' 存在多份,当前生效 {self._skills[meta.name].path.parent}"
                    f"(靠前扫描目录优先,{skills_dir} 中的同名版本被忽略)"
                )
                continue
            self._skills[meta.name] = meta

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

    # ===== 创建接口(createSkill 工具使用)=====

    def rescan(self) -> None:
        """重新扫描 Skill 根目录并原地更新注册表。

        createSkill 落盘后调用,使新 Skill 立即可被 loadSkill 加载;
        版本号自增后,Agent 在下一轮对话开始时自动重建 system 消息。
        """
        if not self._skills_dirs:
            return
        fresh = SkillRegistry.scan(*self._skills_dirs)
        self._skills = fresh._skills
        self.problems = fresh.problems
        self._project_root = fresh._project_root
        self._version += 1

    def create_skill(
        self,
        name: str,
        description: str,
        body: str,
        attachments: list[tuple[str, str]] | None = None,
    ) -> str:
        """在 Skill 根目录下创建新 Skill:<skills_dir>/<name>/SKILL.md + 附件。

        name 同时作为子目录名(小写英文/数字/连字符);SKILL.md 头部自动生成
        YAML Frontmatter,description 以 JSON 风格双引号书写以兼容标准 YAML。
        任一附件路径非法或写入失败都不会留下半成品目录;成功后 rescan 使
        新 Skill 即时可用。返回结果文本(成功清单或失败原因)。
        """
        skills_dir = self._skills_dir or Path.cwd() / ".agents" / "skills"
        name = (name or "").strip()
        if problem := name_problem(name):
            return (
                f"参数错误:{problem};name 同时作为目录名,"
                "请使用小写英文/数字与连字符,如 'code-review'"
            )
        description = " ".join((description or "").split())
        if not description:
            return "参数错误:description 不能为空,请一句话说明技能的用途与适用场景"
        description_truncated = len(description) > _MAX_DESC_LEN
        if description_truncated:
            description = description[:_MAX_DESC_LEN]
        body_text = (body or "").strip()
        if not body_text:
            return "参数错误:content(SKILL.md 正文)不能为空,请提供可执行的操作指引"

        skill_dir = skills_dir / name
        if skill_dir.exists():
            return f"创建失败:同名 Skill 已存在({skill_dir}),请更换 name 或先删除旧目录"

        # 先整体校验附件再落盘,任何一项非法都直接失败,不留下无效 Skill
        prepared: list[tuple[Path, str]] = []
        seen: set[str] = set()
        for index, (filename, content) in enumerate(attachments or [], start=1):
            filename = (filename or "").strip().replace("\\", "/")
            if problem := _attachment_problem(filename, index):
                return problem
            normalized = Path(filename).as_posix()
            if normalized == "SKILL.md":
                return "参数错误:附件不能命名为 SKILL.md(该文件由 name/description/content 自动生成)"
            if normalized in seen:
                return f"参数错误:第 {index} 个附件与之前的文件名重复('{normalized}')"
            seen.add(normalized)
            prepared.append((skill_dir / normalized, content))

        try:
            skill_dir.mkdir(parents=True, exist_ok=False)
            frontmatter = (
                f"---\nname: {name}\n"
                f"description: {json.dumps(description, ensure_ascii=False)}\n---"
            )
            (skill_dir / "SKILL.md").write_text(
                f"{frontmatter}\n\n{body_text}\n", encoding="utf-8"
            )
            for target, content in prepared:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        except OSError as exc:
            shutil.rmtree(skill_dir, ignore_errors=True)  # 清理半成品,避免无效 Skill
            return f"创建失败:文件写入出错({exc})"

        self.rescan()
        lines = [
            f"Skill「{name}」创建成功:",
            f"- 目录: {skill_dir}",
            f"- SKILL.md: frontmatter 已生成,正文 {len(body_text)} 字符",
        ]
        if prepared:
            names = ", ".join(t.relative_to(skill_dir).as_posix() for t, _ in prepared)
            lines.append(f"- 附件 {len(prepared)} 个: {names}")
        if description_truncated:
            lines.append(f"- 注意:description 超过 {_MAX_DESC_LEN} 字符,已截断")
        lines.append(
            "新 Skill 已即时注册,可通过 loadSkill 加载;"
            "重启 xAgent 后会出现在系统提示的技能列表中。"
        )
        return "\n".join(lines)

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
        if problem := name_problem(name):
            self.problems.append(f"跳过 {dir_name}/: {problem}")
            return None
        if not description:
            self.problems.append(f"跳过 {dir_name}/: 缺少必需字段 description")
            return None
        if len(description) > _MAX_DESC_LEN:
            description = description[:_MAX_DESC_LEN]
        return SkillMeta(name=name, description=description, path=path)
