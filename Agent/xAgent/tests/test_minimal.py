"""最小可运行 pytest 用例集（5 个），全部用临时目录，不依赖真实环境。

- 三个文件工具工作区边界用例（越界写入/改名被拒、区内写入成功）；
- createSkill → loadSkill 闭环；
- load_mcp_config 对非法 JSON / 缺 mcpServers 的处理。
"""

import json
from pathlib import Path

from tools.file_tools import rename_path, write_file
from tools.mcp_tools import load_mcp_config
from tools.skill_tools import _make_create_handler, _make_load_handler


def test_write_outside_workspace_is_rejected(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("XAGENT_WRITE_ROOTS", raising=False)

    result = write_file({"path": str(outside), "content": "changed"})

    assert "安全限制" in result
    assert "XAGENT_WRITE_ROOTS" in result
    assert outside.read_text(encoding="utf-8") == "original"


def test_write_inside_workspace_succeeds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XAGENT_WRITE_ROOTS", raising=False)
    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")

    result = write_file({"path": str(target), "content": "new"})

    assert "已写入文件" in result
    assert target.read_text(encoding="utf-8") == "new"


def test_rename_outside_workspace_is_rejected(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_text("content", encoding="utf-8")
    destination = tmp_path / "destination.txt"
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("XAGENT_WRITE_ROOTS", raising=False)

    result = rename_path({"old_path": str(source), "new_path": str(destination)})

    assert "安全限制" in result
    assert source.is_file()
    assert not destination.exists()


def test_create_then_load_skill_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    from core.skills import SkillRegistry

    registry = SkillRegistry.scan(tmp_path / ".agents" / "skills")
    create_skill = _make_create_handler(registry)
    load_skill = _make_load_handler(registry)

    created = create_skill(
        {"name": "demo", "description": "演示技能", "content": "请使用演示步骤完成任务。"}
    )
    assert "创建成功" in created
    assert (tmp_path / ".agents" / "skills" / "demo" / "SKILL.md").is_file()

    loaded = load_skill({"name": "demo"})
    assert "演示步骤" in loaded


def test_load_mcp_config_invalid_json_and_missing_key(tmp_path: Path) -> None:
    broken = tmp_path / ".mcp.json"
    broken.write_text("{ not valid json", encoding="utf-8")
    servers, problems = load_mcp_config(tmp_path)
    assert servers == {}
    assert problems and "解析失败" in problems[0]

    broken.write_text(json.dumps({"other": 1}), encoding="utf-8")
    servers, problems = load_mcp_config(tmp_path)
    assert servers == {}
    assert problems and "mcpServers" in problems[0]
