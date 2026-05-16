"""freerun_allowlist.py — freerun mode 命令白名单 + tier 分级.

覆盖:
  - allow_prefixes 命中 → ALLOW T1
  - blacklist_override (credential path / dangerous_cmd / deny_subcommand) → DENY 升 tier
  - deny_tokens (>, ;, &&, $(...), backtick) → DENY T2
  - pipe_right_deny → DENY T2
  - pipe > 5 段 → DENY T2 (pipe_too_deep)
  - 不命中 allow + 无 blacklist → ASK T2 (no_allowlist_match)
  - 空 cmd / 缺 config → ASK
  - kill switch (FREERUN_KILL_SWITCH=1 / flag file) → ASK
  - 异常 → ASK fail-closed
"""
from __future__ import annotations
import importlib
import json
import os
from pathlib import Path

import pytest


SAMPLE_CFG = {
    "allow_prefixes": {
        "git_read": ["git status", "git log", "git diff"],
        "fs_read": ["ls", "cat ", "head", "tail", "pwd"],
    },
    "blacklist_override": {
        "credential_paths": ["/.ssh/id_rsa", "~/.aws/credentials"],
        "credential_glob_pcre": r"id_(rsa|ed25519)\b",
        "llm_token_glob_pcre": r"anthropic[_-]?api[_-]?key",
        "llm_token_paths": ["~/.anthropic/token"],
        "proc_sensitive": [r"/proc/\d+/environ"],
        "deny_subcommands": ["restart"],
        "dangerous_cmds": ["sudo", "rm"],
    },
    "pipe_right_deny": ["sh", "bash", "curl"],
    "tier_classification": {
        "blacklist_credential_path": "T4",
        "blacklist_proc_sensitive": "T4",
        "blacklist_deny_subcommand": "T3",
        "blacklist_dangerous_cmd": "T3",
        "no_match_fall_through": "T2",
    },
}


def _fresh_allowlist(tmp_path, cfg=SAMPLE_CFG, kill_switch_file=None):
    rule_path = tmp_path / "freerun_allowlist.json"
    rule_path.write_text(json.dumps(cfg))
    ks = kill_switch_file or (tmp_path / "kill_switch.flag")
    os.environ["PRE_FREERUN_ALLOWLIST"] = str(rule_path)
    os.environ["PRE_FREERUN_KILL_SWITCH_FILE"] = str(ks)
    os.environ.pop("FREERUN_KILL_SWITCH", None)

    import sys
    sys.modules.pop("freerun_allowlist", None)
    return importlib.import_module("freerun_allowlist"), rule_path, ks


def test_empty_cmd_asks(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, reason, tier = fa.check("")
    assert decision == fa.ASK
    assert reason == "empty_cmd"


def test_config_missing_returns_ask(tmp_path):
    """config 路径不存在 → fail-closed ASK."""
    os.environ["PRE_FREERUN_ALLOWLIST"] = str(tmp_path / "absent.json")
    os.environ["PRE_FREERUN_KILL_SWITCH_FILE"] = str(tmp_path / "ks.flag")
    import sys
    sys.modules.pop("freerun_allowlist", None)
    fa = importlib.import_module("freerun_allowlist")
    decision, reason, _ = fa.check("git status")
    assert decision == fa.ASK
    assert reason == "config_missing_or_bad"


def test_allow_prefix_t1(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, reason, tier = fa.check("git status -s")
    assert decision == fa.ALLOW
    assert tier == "T1"
    assert "git_read" in reason


def test_deny_tokens_block_redirect(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, reason, tier = fa.check("ls > /tmp/x")
    assert decision == fa.DENY
    assert reason == "deny_tokens_pcre"
    assert tier == "T2"


@pytest.mark.parametrize("cmd", [
    "ls && rm file",
    "ls; pwd",
    "echo $(whoami)",
    "echo `whoami`",
    "ls || pwd",
])
def test_deny_tokens_compound_shells(tmp_path, cmd):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, _, _ = fa.check(cmd)
    assert decision == fa.DENY


def test_blacklist_dangerous_cmd_t3(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, reason, tier = fa.check("rm file.txt")
    assert decision == fa.DENY
    assert tier == "T3"
    assert "dangerous_cmd" in reason


def test_blacklist_credential_path_t4(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    # cat ~/.ssh/id_rsa — 不含 deny token, allow_prefix `cat ` 命中但黑名单先
    decision, reason, tier = fa.check("cat /.ssh/id_rsa")
    assert decision == fa.DENY
    assert tier == "T4"
    assert "credential_path" in reason


def test_blacklist_credential_glob_pcre(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, reason, tier = fa.check("ls id_rsa")
    assert decision == fa.DENY
    assert tier == "T4"


def test_pipe_right_deny(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, reason, tier = fa.check("cat README.md | sh")
    assert decision == fa.DENY
    assert "pipe_right_deny:sh" in reason
    assert tier == "T2"


def test_pipe_too_deep(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    cmd = " | ".join(["ls"] * 7)
    decision, reason, _ = fa.check(cmd)
    assert decision == fa.DENY
    assert reason == "pipe_too_deep"


def test_no_match_falls_through_to_ask(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, reason, tier = fa.check("some-unknown-binary --flag")
    assert decision == fa.ASK
    assert reason == "no_allowlist_match"
    assert tier == "T2"


def test_kill_switch_env_var(tmp_path):
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    os.environ["FREERUN_KILL_SWITCH"] = "1"
    try:
        decision, reason, _ = fa.check("git status")
        assert decision == fa.ASK
        assert reason == "kill_switch_active"
    finally:
        os.environ.pop("FREERUN_KILL_SWITCH", None)


def test_kill_switch_file_flag(tmp_path):
    ks = tmp_path / "ks.flag"
    fa, _r, _ks = _fresh_allowlist(tmp_path, kill_switch_file=ks)
    ks.write_text("active")
    decision, reason, _ = fa.check("git status")
    assert decision == fa.ASK
    assert reason == "kill_switch_active"


def test_blacklist_takes_priority_over_allowlist(tmp_path):
    """`rm` 同时命中 allow_prefix 想象的子串 + blacklist; blacklist 优先."""
    fa, _r, _ks = _fresh_allowlist(tmp_path)
    decision, _, _ = fa.check("rm file")
    assert decision == fa.DENY
