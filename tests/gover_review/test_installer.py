"""install_gover_review.py — installer entry 单测.

不真调 pre_init / fire trigger (subprocess); 通过 skip_* flag 隔离, 只测:
  resolve_pre_rule_root      env / fallback / quoted value
  main()                     端到端 idempotent (跑两遍 schedules.json 不重复)
  main() 模板真实安装到 workdir
  fire_initial_trigger       trigger 文件缺 → warn 不 raise
  run_pre_init               pre_init.py 缺 → 返非 0
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_PRE_ROOT = _TESTS_DIR.parent.parent
sys.path.insert(0, str(_PRE_ROOT / "scripts"))

import install_gover_review as inst  # noqa: E402


# ---------- resolve_pre_rule_root ----------

def test_resolve_pre_rule_root_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = tmp_path / ".pre" / "env"
    env.parent.mkdir(parents=True)
    env.write_text(f"PRE_RULE_ROOT={tmp_path}/custom_rule\n")
    assert inst.resolve_pre_rule_root() == tmp_path / "custom_rule"


def test_resolve_pre_rule_root_quoted_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = tmp_path / ".pre" / "env"
    env.parent.mkdir(parents=True)
    env.write_text(f'PRE_RULE_ROOT="{tmp_path}/q_rule" # inline comment\n')
    assert inst.resolve_pre_rule_root() == tmp_path / "q_rule"


def test_resolve_pre_rule_root_no_env_fallback_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    # ~/.pre/env 缺 → fallback to pre_root/../pre_rule
    out = inst.resolve_pre_rule_root()
    assert out.name == "pre_rule"


def test_resolve_pre_rule_root_ignores_unrelated_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = tmp_path / ".pre" / "env"
    env.parent.mkdir(parents=True)
    env.write_text(
        "# comment\n"
        "PRE_ROOT=/x\n"
        "PRE_HOOK_SECRET=secret123\n"
        f"PRE_RULE_ROOT={tmp_path}/rl\n"
        "PRE_LOG_DIR=/y\n"
    )
    assert inst.resolve_pre_rule_root() == tmp_path / "rl"


# ---------- run_pre_init ----------

def test_run_pre_init_missing_script_warns(tmp_path, capsys):
    fake_root = tmp_path / "nope"
    fake_root.mkdir()
    rc = inst.run_pre_init(tmp_path / "wd", pre_root=fake_root)
    assert rc == 1
    assert "pre_init.py not found" in capsys.readouterr().out


# ---------- fire_initial_trigger ----------

def test_fire_initial_trigger_missing_script_warns(tmp_path, capsys):
    rc = inst.fire_initial_trigger(tmp_path / "no_trigger.sh")
    assert rc == 1
    assert "trigger script missing" in capsys.readouterr().out


# ---------- main end-to-end ----------

def test_main_idempotent_no_duplicate_entry(tmp_path):
    wd = tmp_path / "wd"
    rule = tmp_path / "rule"
    fake_pre = tmp_path / "pre"
    # 造 fake trigger 让 fire 不 warn (skip trigger 仍跳过执行)
    (fake_pre / "scripts" / "gover_review").mkdir(parents=True)
    (fake_pre / "scripts" / "gover_review" / "cron_trigger.sh").write_text(
        "#!/usr/bin/env bash\n"
    )

    for _ in range(2):
        rc = inst.main(
            workdir=wd,
            pre_root=fake_pre,
            rule_root=rule,
            skip_pre_init=True,
            skip_initial_trigger=True,
        )
        assert rc == 0

    s = json.loads((rule / "cron" / "schedules.json").read_text())
    matching = [x for x in s["schedules"] if x["id"] == "gover-review-4h"]
    assert len(matching) == 1


def test_main_installs_real_templates_to_workdir(tmp_path):
    wd = tmp_path / "wd"
    rule = tmp_path / "rule"
    fake_pre = tmp_path / "pre"
    (fake_pre / "scripts" / "gover_review").mkdir(parents=True)
    (fake_pre / "scripts" / "gover_review" / "cron_trigger.sh").write_text(
        "#!/usr/bin/env bash\n"
    )

    rc = inst.main(
        workdir=wd,
        pre_root=fake_pre,
        rule_root=rule,
        skip_pre_init=True,
        skip_initial_trigger=True,
    )
    assert rc == 0
    # 真实模板从 _PRE_ROOT/scripts/gover_review/templates/ 拉, 不是 fake_pre
    assert (wd / "pre" / "agent_config.json").exists()
    assert (wd / "pre" / "next.md").exists()
    assert (wd / "pre" / "rules.md").exists()
    cfg = json.loads((wd / "pre" / "agent_config.json").read_text())
    assert cfg["role"] == "gover_review"
    assert cfg["cli"] == "claude"


def test_main_schedule_points_to_trigger_in_given_pre_root(tmp_path):
    wd = tmp_path / "wd"
    rule = tmp_path / "rule"
    fake_pre = tmp_path / "alt_pre"
    (fake_pre / "scripts" / "gover_review").mkdir(parents=True)
    (fake_pre / "scripts" / "gover_review" / "cron_trigger.sh").write_text(
        "#!/usr/bin/env bash\n"
    )

    inst.main(
        workdir=wd,
        pre_root=fake_pre,
        rule_root=rule,
        skip_pre_init=True,
        skip_initial_trigger=True,
    )
    s = json.loads((rule / "cron" / "schedules.json").read_text())
    entry = [x for x in s["schedules"] if x["id"] == "gover-review-4h"][0]
    assert entry["cmd"][1] == str(
        fake_pre / "scripts" / "gover_review" / "cron_trigger.sh"
    )


def test_main_uses_default_workdir_path():
    """sanity: DEFAULT_WORKDIR 指向 ~/.pre/internal_agents/gover_review"""
    assert str(inst.DEFAULT_WORKDIR).endswith(
        "/.pre/internal_agents/gover_review"
    )
