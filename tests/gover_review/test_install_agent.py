"""install_agent.py — 模板安装单测.

覆盖:
  template_files          模板目录存在且含 agent_config.json / next.md / rules.md
  agent_config schema     mode=enforce, cli=claude, role=gover_review
  install_workdir 首次    创建 pre_dir, copy 所有模板, findings/ 空目录
  install_workdir 幂等    第二次跑 — 全部 skipped
  install_workdir force   force=True 覆盖已有
  install_workdir missing 模板目录不存在 → 返空 created
"""
from __future__ import annotations

import json
from pathlib import Path

from gover_review.install_agent import (
    DEFAULT_WORKDIR,
    TEMPLATES_DIR,
    install_workdir,
    template_files,
)


# ---------- 模板真实文件 sanity ----------

def test_templates_dir_exists():
    assert TEMPLATES_DIR.exists(), f"templates dir missing: {TEMPLATES_DIR}"


def test_template_files_includes_required():
    names = {p.name for p in template_files()}
    assert "agent_config.json" in names
    assert "next.md" in names
    assert "rules.md" in names


def test_agent_config_schema():
    cfg_path = TEMPLATES_DIR / "agent_config.json"
    cfg = json.loads(cfg_path.read_text())
    # agent_config.mode = 自主等级 (supervised), 不是 cfg.mode (enforce)
    assert cfg["mode"] == "supervised"
    assert cfg["cli"] == "claude"
    assert cfg["role"] == "gover_review"


def test_next_md_mentions_state_machine():
    text = (TEMPLATES_DIR / "next.md").read_text()
    assert "工作循环" in text
    assert "pending_finding_path" in text
    assert "codex -p" in text
    assert "target_layer" in text


def test_rules_md_forbids_editing_rules_py():
    text = (TEMPLATES_DIR / "rules.md").read_text()
    assert "rules.py" in text
    assert "patch" in text.lower()


# ---------- install_workdir 行为 ----------

def test_install_workdir_first_run(tmp_path):
    wd = tmp_path / "gover_review_wd"
    r = install_workdir(wd)
    assert r["errors"] == []
    assert r["skipped"] == []
    assert len(r["created"]) >= 3  # agent_config + next + rules
    assert (wd / "pre" / "agent_config.json").exists()
    assert (wd / "pre" / "next.md").exists()
    assert (wd / "pre" / "rules.md").exists()
    assert (wd / "pre" / "findings").is_dir()


def test_install_workdir_idempotent(tmp_path):
    wd = tmp_path / "gover_review_wd"
    install_workdir(wd)
    r = install_workdir(wd)
    assert r["errors"] == []
    assert r["created"] == []
    assert len(r["skipped"]) >= 3


def test_install_workdir_force_overwrites(tmp_path):
    wd = tmp_path / "gover_review_wd"
    install_workdir(wd)
    # 用户改了 agent_config — force 应覆盖回模板
    custom = wd / "pre" / "agent_config.json"
    custom.write_text('{"mode": "freerun"}\n')
    r = install_workdir(wd, force=True)
    assert r["errors"] == []
    assert any("agent_config.json" in c for c in r["created"])
    cfg = json.loads(custom.read_text())
    assert cfg["mode"] == "supervised"
    assert cfg.get("role") == "gover_review"


def test_install_workdir_missing_templates_dir(tmp_path):
    wd = tmp_path / "gover_review_wd"
    fake_templates = tmp_path / "no_such_templates"
    r = install_workdir(wd, templates_dir=fake_templates)
    assert r["errors"] == []
    assert r["created"] == []
    # pre_dir 仍然建出来
    assert (wd / "pre" / "findings").is_dir()


def test_default_workdir_points_to_internal_agents():
    s = str(DEFAULT_WORKDIR)
    assert s.endswith("/.pre/internal_agents/gover_review")
