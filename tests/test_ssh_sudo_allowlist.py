"""ssh_sudo_allowlist.py — ssh+sudo 非写入 allowlist 层 (黑优于白, 不命中走 governor).

覆盖:
  - 非 ssh/sudo cmd → GOVERNOR (不归本层)
  - config 缺失 → GOVERNOR
  - allow_prefixes 命中 → ALLOW (sudo/ssh wrapped 都行)
  - blacklist credential_path / dangerous_cmd / deny_subcommand → DENY
  - deny_tokens (>, ;, &&, $(...), backtick) → DENY
  - 单 pipe 右侧 pipe_right_deny → DENY
  - ssh host 'inner' 拆解, inner 走黑/白检查
  - 不命中 → GOVERNOR (fail-safe)
"""
from __future__ import annotations
import importlib
import json
import os
import sys
from pathlib import Path

import pytest


SAMPLE_CFG = {
    "deny_tokens_pcre": r"(?<!\\)(>>?|<<?|<<<|>&|&>|2>&1|\$\(|`|<\(|>\(|;|&&|\|\||&\s*$)",
    "allow_prefixes": {
        "log_read": ["sudo tail", "sudo cat /var/log", "sudo journalctl"],
        "service_status": ["sudo systemctl status", "sudo pm2 list"],
        "remote_read": ["ls", "cat", "head", "tail", "pm2 list",
                         "systemctl status", "docker ps"],
    },
    "blacklist_override": {
        "credential_paths": ["/.ssh/id_rsa", "/etc/shadow"],
        "credential_glob_pcre": r"id_(rsa|ed25519)\b",
        "llm_token_paths": ["~/.anthropic/token"],
        "llm_token_glob_pcre": r"anthropic[_-]?api[_-]?key",
        "proc_sensitive": [r"/proc/\d+/environ"],
        "deny_subcommands": {
            "systemctl": ["restart", "stop"],
            "pm2": ["restart", "kill"],
        },
        "dangerous_cmds": ["rm", "dd", "mkfs"],
    },
    "pipe_right_deny": ["sh", "bash", "curl"],
}


def _fresh_module(tmp_path, cfg=SAMPLE_CFG):
    """重 import ssh_sudo_allowlist, monkeypatch CONFIG_PATH 指向 tmp."""
    config_dir = tmp_path / "hook"
    config_dir.mkdir()
    config_path = config_dir / "ssh_sudo_allowlist.json"
    config_path.write_text(json.dumps(cfg))

    sys.modules.pop("ssh_sudo_allowlist", None)
    mod = importlib.import_module("ssh_sudo_allowlist")
    # 改 module 全局指针 + 清缓存
    mod.CONFIG_PATH = config_path
    mod._CONFIG_CACHE = None
    mod._CONFIG_MTIME = None
    mod._DENY_TOKENS_RE = None
    mod._CRED_GLOB_RE = None
    mod._LLM_GLOB_RE = None
    mod._PROC_RE = None
    return mod


def test_non_ssh_sudo_cmd_returns_governor(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check("git status")
    assert decision == m.GOVERNOR
    assert reason == "not_ssh_sudo"


def test_missing_config_returns_governor(tmp_path):
    m = _fresh_module(tmp_path)
    m.CONFIG_PATH = tmp_path / "does-not-exist.json"
    m._CONFIG_CACHE = None
    decision, reason, _ = m.check("sudo cat /var/log/system.log")
    assert decision == m.GOVERNOR
    assert reason == "config_missing_or_bad"


def test_sudo_allowed_prefix(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, rule = m.check("sudo tail -n 100 /var/log/system.log")
    assert decision == m.ALLOW
    assert "log_read" in reason
    assert rule.startswith("allowlist:")


def test_sudo_systemctl_status_allowed(tmp_path):
    m = _fresh_module(tmp_path)
    decision, _, _ = m.check("sudo systemctl status nginx")
    assert decision == m.ALLOW


def test_sudo_dangerous_rm_denied(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check("sudo rm /tmp/file")
    assert decision == m.DENY
    assert "dangerous_cmd" in reason


def test_sudo_deny_subcommand(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check("sudo systemctl restart nginx")
    assert decision == m.DENY
    assert "deny_subcommand" in reason


def test_sudo_credential_path_denied(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check("sudo cat /etc/shadow")
    assert decision == m.DENY
    assert "credential_path" in reason


@pytest.mark.parametrize("cmd", [
    "sudo cat /var/log/x.log > /tmp/leak",
    "sudo cat /var/log/x.log && rm /tmp/x",
    "sudo cat /var/log/x.log; ls",
    "sudo cat $(ls)",
    "sudo cat `whoami`",
])
def test_deny_tokens_blocked(tmp_path, cmd):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check(cmd)
    assert decision == m.DENY
    assert reason == "deny_tokens_pcre"


def test_ssh_wrapped_inner_allowed(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check("ssh host 'ls /tmp'")
    assert decision == m.ALLOW
    assert "remote_read" in reason


def test_ssh_wrapped_inner_blocked_by_blacklist(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check("ssh host 'rm -rf /'")
    assert decision == m.DENY


def test_pipe_right_deny_blocks_sh(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check("sudo cat /var/log/install.log | sh")
    assert decision == m.DENY
    assert "pipe_right_deny" in reason


def test_pipe_first_segment_decides(tmp_path):
    """pipe 第一段 sudo tail 命中白, 右侧不在 deny → allow."""
    m = _fresh_module(tmp_path)
    decision, _, _ = m.check("sudo tail /var/log/x | head -1")
    # 第一段是 'sudo tail /var/log/x' allow_prefixes 命中
    assert decision == m.ALLOW


def test_too_many_pipes_denied(tmp_path):
    m = _fresh_module(tmp_path)
    cmd = " | ".join(["sudo tail /var/log/a"] + ["head"] * 6)
    decision, reason, _ = m.check(cmd)
    assert decision == m.DENY
    assert reason == "too_many_pipes"


def test_unmatched_falls_to_governor(tmp_path):
    m = _fresh_module(tmp_path)
    decision, reason, _ = m.check("sudo unknown-binary --flag")
    assert decision == m.GOVERNOR
    assert reason == "no_allowlist_match"


def test_check_with_audit_writes_jsonl(tmp_path, monkeypatch):
    m = _fresh_module(tmp_path)
    audit_dir = tmp_path / "audit"
    monkeypatch.setattr(m, "AUDIT_DIR", audit_dir)
    decision, _, _ = m.check_with_audit("sudo tail /var/log/x.log",
                                          agent_id="agent-A")
    assert decision == m.ALLOW
    files = list(audit_dir.glob("ssh_sudo_audit_*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text().strip())
    assert entry["agent_id"] == "agent-A"
    assert entry["decision"] == "allow"
