"""pytest 共享配置 — 把 src/ 跟 pre_mcp/ 挂到 sys.path, 隔离全局状态.

设计原则:
  - 每个 test 都跑在 isolated tmp_path 下, 不污染真实 ~/.pre/env / pre_rule / pre_log
  - module-level cache (token_resolver._LOADED / ssh_sudo_allowlist._CONFIG_*) 通过
    fixture autouse 清零, 防 test 间互相影响
  - test 不需要 import mcp SDK; 只测纯 Python state machine
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
PRE_MCP_DIR = PROJECT_ROOT / "pre_mcp"

for p in (str(SRC_DIR), str(PRE_MCP_DIR), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _isolate_pre_env(tmp_path, monkeypatch):
    """每个 test 默认指向 tmp_path 下的隔离 ~/.pre/env + pre_rule + pre_log.

    覆盖 token_resolver / common.paths / freerun_* 用的 env 入口.
    real_home fixture 想用真 ~/.pre/env 的 test 可以 monkeypatch.undo() 之后再 setenv.
    """
    fake_home = tmp_path / "home"
    fake_pre = fake_home / ".pre"
    fake_pre.mkdir(parents=True)
    (fake_pre / "env").write_text("")
    monkeypatch.setenv("HOME", str(fake_home))

    fake_rule = tmp_path / "pre_rule"
    fake_log = tmp_path / "pre_log"
    fake_rule.mkdir()
    fake_log.mkdir()
    monkeypatch.setenv("PRE_RULE_ROOT", str(fake_rule))
    monkeypatch.setenv("PRE_LOG_DIR", str(fake_log))
    monkeypatch.setenv("PRE_AGENT_HOME", str(tmp_path / "agents"))

    # token_resolver: 复位 _LOADED, 让下一次 resolve() 重新读 env
    if "common.token_resolver" in sys.modules:
        sys.modules["common.token_resolver"]._LOADED = False  # type: ignore[attr-defined]

    yield


@pytest.fixture
def write_pre_env(tmp_path, monkeypatch):
    """工厂 fixture: write_pre_env({"PRE_HOOK_SECRET": "xxx"}) — 写入 isolated ~/.pre/env."""

    def _write(kv: dict[str, str]) -> Path:
        env_file = Path(os.environ["HOME"]) / ".pre" / "env"
        lines = [f"{k}={v}" for k, v in kv.items()]
        env_file.write_text("\n".join(lines) + "\n")
        # 同步清掉对应 environ key, 让 resolver fresh load
        for k in kv:
            monkeypatch.delenv(k, raising=False)
        if "common.token_resolver" in sys.modules:
            sys.modules["common.token_resolver"]._LOADED = False  # type: ignore[attr-defined]
        return env_file

    return _write
