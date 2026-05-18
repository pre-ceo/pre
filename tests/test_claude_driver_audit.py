"""claude_driver_audit — PreToolUse hook 决策的 jsonl audit.

覆盖:
  - resolve_agent_from_cwd: 3 级优先级 + 缺/坏配置 fail-safe
  - tool_preview: Bash/Read/Edit/Grep/WebFetch/未知 工具的 240 字符上限
  - audit_decision: jsonl 写出来 schema 完整, fail-safe (cwd 缺/坏)
  - audit_view.py 读出来 driver=claude, fields 白名单生效
"""
from __future__ import annotations
import json
import os
from pathlib import Path

import pytest

from claude_driver_audit import (
    resolve_agent_from_cwd,
    tool_preview,
    audit_decision,
)


# -------- resolve_agent_from_cwd --------

def _write_agent_config(cwd: Path, cfg: dict) -> None:
    (cwd / "pre").mkdir(parents=True, exist_ok=True)
    (cwd / "pre" / "agent_config.json").write_text(json.dumps(cfg))


def test_resolve_empty_cwd():
    assert resolve_agent_from_cwd("") == ("", "")


def test_resolve_missing_config(tmp_path):
    assert resolve_agent_from_cwd(str(tmp_path)) == ("", "")


def test_resolve_corrupt_json(tmp_path):
    (tmp_path / "pre").mkdir()
    (tmp_path / "pre" / "agent_config.json").write_text("{not json")
    assert resolve_agent_from_cwd(str(tmp_path)) == ("", "")


def test_resolve_driver_type_project_name(tmp_path, monkeypatch):
    monkeypatch.setenv("PRE_NODE_ID", "local")
    _write_agent_config(tmp_path, {
        "driver_type": "cli-claude-code-local",
        "project_name": "pre_ui",
        "tmux_session": "pre_ui",
    })
    aid, tmux = resolve_agent_from_cwd(str(tmp_path))
    assert aid == "local.cli-claude-code-local.pre_ui"
    assert tmux == "pre_ui"


def test_resolve_mcp_caller_agent_id_overrides(tmp_path, monkeypatch):
    """mcp.caller_agent_id 显式时优先于 driver_type+project_name."""
    monkeypatch.setenv("PRE_NODE_ID", "local")
    _write_agent_config(tmp_path, {
        "driver_type": "cli-claude-code-local",
        "project_name": "wrong",
        "mcp": {"caller_agent_id": "local.cli-claude-code-local.explicit"},
        "tmux_session": "explicit",
    })
    aid, _ = resolve_agent_from_cwd(str(tmp_path))
    assert aid == "local.cli-claude-code-local.explicit"


def test_resolve_top_level_agent_id_fallback(tmp_path, monkeypatch):
    """charter-registered agent: 只有顶层 agent_id, 仍接受."""
    monkeypatch.setenv("PRE_NODE_ID", "local")
    _write_agent_config(tmp_path, {
        "agent_id": "local.cli-claude-code-local.charter_agent",
    })
    aid, tmux = resolve_agent_from_cwd(str(tmp_path))
    assert aid == "local.cli-claude-code-local.charter_agent"
    assert tmux == ""


def test_resolve_top_level_agent_id_wrong_node_rejected(tmp_path, monkeypatch):
    """顶层 agent_id 前缀不是本节点 → 拒 (防 cross-node 伪造)."""
    monkeypatch.setenv("PRE_NODE_ID", "local")
    _write_agent_config(tmp_path, {
        "agent_id": "remote-node.cli-claude-code-local.foreign",
    })
    aid, _ = resolve_agent_from_cwd(str(tmp_path))
    assert aid == ""


def test_resolve_partial_driver_only(tmp_path, monkeypatch):
    """只 driver_type 没 project_name → 配置不完整, 返 空."""
    monkeypatch.setenv("PRE_NODE_ID", "local")
    _write_agent_config(tmp_path, {"driver_type": "cli-claude-code-local"})
    assert resolve_agent_from_cwd(str(tmp_path)) == ("", "")


def test_resolve_non_dict_config(tmp_path):
    """agent_config.json 是 list/null → fail-safe."""
    (tmp_path / "pre").mkdir()
    (tmp_path / "pre" / "agent_config.json").write_text("[1,2,3]")
    assert resolve_agent_from_cwd(str(tmp_path)) == ("", "")


# -------- tool_preview --------

def test_preview_bash():
    assert tool_preview("Bash", {"command": "ls -la"}) == "ls -la"


def test_preview_bash_truncated_240():
    cmd = "echo " + "x" * 500
    assert len(tool_preview("Bash", {"command": cmd})) == 240


def test_preview_read_write_edit():
    assert tool_preview("Read", {"file_path": "/a/b.txt"}) == "/a/b.txt"
    assert tool_preview("Write", {"file_path": "/c/d.py"}) == "/c/d.py"
    assert tool_preview("Edit", {"file_path": "/e/f.md"}) == "/e/f.md"
    assert tool_preview("MultiEdit", {"file_path": "/g.py"}) == "/g.py"


def test_preview_grep_glob():
    assert tool_preview("Grep", {"pattern": "foo.*bar"}) == "foo.*bar"
    assert tool_preview("Glob", {"pattern": "**/*.py"}) == "**/*.py"


def test_preview_web():
    assert tool_preview("WebFetch", {"url": "https://x.com"}) == "https://x.com"
    assert tool_preview("WebSearch", {"query": "foo"}) == "foo"


def test_preview_unknown_tool_json_dumps():
    out = tool_preview("UnknownTool", {"a": 1, "b": "hi"})
    parsed = json.loads(out)
    assert parsed == {"a": 1, "b": "hi"}


def test_preview_unknown_tool_non_serializable():
    """非可序列化 input → 返空字符串, 不抛."""
    class _X:
        pass
    assert tool_preview("Foo", {"obj": _X()}) == ""


# -------- audit_decision (jsonl 写入) --------

def _read_audit_lines(log_dir: Path) -> list[dict]:
    audit_dir = log_dir / "claude_driver"
    if not audit_dir.is_dir():
        return []
    out = []
    for f in audit_dir.glob("auto_decision_*.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_audit_writes_full_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("PRE_NODE_ID", "local")
    cwd = tmp_path / "agents" / "pre_ui"
    cwd.mkdir(parents=True)
    _write_agent_config(cwd, {
        "driver_type": "cli-claude-code-local",
        "project_name": "pre_ui",
        "tmux_session": "pre_ui",
    })
    input_data = {
        "cwd": str(cwd),
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    }
    result = {"source": "local"}
    audit_decision(input_data, result, "allow", "git status 在白名单内")

    log_root = Path(os.environ["PRE_LOG_DIR"])
    rows = _read_audit_lines(log_root)
    assert len(rows) == 1
    r = rows[0]
    assert r["agent_id"] == "local.cli-claude-code-local.pre_ui"
    assert r["tmux_session"] == "pre_ui"
    assert r["tool_name"] == "Bash"
    assert r["tool_input_preview"] == "git status"
    assert r["decision"] == "allow"
    assert r["reason"] == "git status 在白名单内"
    assert r["source"] == "local"
    assert r["action"] == "hook_decision"
    assert r["ok"] is True
    assert "ts" in r and r["ts"]
    # cwd 字段 ** 不应** 写入 (audit_view 白名单已排除, 这里也别写)
    assert "cwd" not in r
    # driver 字段不写, 由 audit_view 从目录名衍生
    assert "driver" not in r


def test_audit_missing_agent_config_still_writes(tmp_path):
    """cwd 没 agent_config.json → agent_id/tmux 留空, 仍写一条 audit."""
    cwd = tmp_path / "orphan"
    cwd.mkdir()
    audit_decision(
        {"cwd": str(cwd), "tool_name": "Read",
         "tool_input": {"file_path": "/x"}},
        {"source": "local"}, "allow", "")
    rows = _read_audit_lines(Path(os.environ["PRE_LOG_DIR"]))
    assert len(rows) == 1
    assert rows[0]["agent_id"] == ""
    assert rows[0]["tmux_session"] == ""
    assert rows[0]["tool_name"] == "Read"


def test_audit_failsafe_on_unwritable_dir(tmp_path, monkeypatch):
    """PRE_LOG_DIR 不可写 → 不抛, 静默吞."""
    bad = tmp_path / "ro"
    bad.mkdir()
    bad.chmod(0o500)  # read-only
    monkeypatch.setenv("PRE_LOG_DIR", str(bad))
    try:
        # 不应抛
        audit_decision({"cwd": "", "tool_name": "Bash",
                        "tool_input": {"command": "x"}},
                       {}, "ask", "")
    finally:
        bad.chmod(0o700)


def test_audit_appends_multiple(tmp_path):
    """同一文件多次 append, 不覆盖."""
    cwd = tmp_path / "a"
    cwd.mkdir()
    for i in range(3):
        audit_decision(
            {"cwd": str(cwd), "tool_name": "Bash",
             "tool_input": {"command": f"echo {i}"}},
            {"source": "cache"}, "allow", f"r{i}")
    rows = _read_audit_lines(Path(os.environ["PRE_LOG_DIR"]))
    assert len(rows) == 3
    assert [r["reason"] for r in rows] == ["r0", "r1", "r2"]


def test_audit_jsonl_file_chmod_600(tmp_path):
    cwd = tmp_path / "a"
    cwd.mkdir()
    audit_decision({"cwd": str(cwd), "tool_name": "Bash",
                    "tool_input": {"command": "ls"}},
                   {}, "allow", "")
    audit_dir = Path(os.environ["PRE_LOG_DIR"]) / "claude_driver"
    files = list(audit_dir.glob("auto_decision_*.jsonl"))
    assert files, "audit jsonl 没写出来"
    mode = files[0].stat().st_mode & 0o777
    assert mode == 0o600


# -------- audit_view 联调 --------

def test_audit_view_reads_claude_driver(tmp_path, monkeypatch):
    """写一条 audit, audit_view.read_entries('driver_decision') 读出来 driver='claude'."""
    monkeypatch.setenv("PRE_NODE_ID", "local")
    cwd = tmp_path / "agents" / "pre_ui"
    cwd.mkdir(parents=True)
    _write_agent_config(cwd, {
        "driver_type": "cli-claude-code-local",
        "project_name": "pre_ui",
        "tmux_session": "pre_ui",
    })
    audit_decision(
        {"cwd": str(cwd), "tool_name": "Bash",
         "tool_input": {"command": "ls"}},
        {"source": "local"}, "allow", "ok")

    from master.audit_view import read_entries, KINDS
    # 元数据校验
    assert "claude_driver" in KINDS["driver_decision"]["dirs"]

    log_root = Path(os.environ["PRE_LOG_DIR"])
    rows, truncated = read_entries(
        "driver_decision", since=0, limit=10, filters={},
        log_root=log_root)
    assert len(rows) == 1
    assert rows[0]["driver"] == "claude"
    assert rows[0]["agent_id"] == "local.cli-claude-code-local.pre_ui"
    assert rows[0]["tool_name"] == "Bash"
    assert rows[0]["decision"] == "allow"
    assert truncated is False


def test_audit_view_filters_by_driver(tmp_path, monkeypatch):
    """driver=claude 过滤命中, driver=codex 不命中."""
    cwd = tmp_path / "a"
    cwd.mkdir()
    audit_decision({"cwd": str(cwd), "tool_name": "Bash",
                    "tool_input": {"command": "ls"}},
                   {"source": "local"}, "allow", "")

    from master.audit_view import read_entries
    log_root = Path(os.environ["PRE_LOG_DIR"])
    rows_claude, _ = read_entries("driver_decision", 0, 10,
                                   {"driver": "claude"}, log_root)
    assert len(rows_claude) == 1
    rows_codex, _ = read_entries("driver_decision", 0, 10,
                                  {"driver": "codex"}, log_root)
    assert rows_codex == []
