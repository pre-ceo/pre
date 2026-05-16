"""common/token_resolver.py — ~/.pre/env loader + kind→env mapping.

覆盖:
  - 缺 env file → TokenNotFound (kind 已知, env_key 不存在)
  - env file 写 PRE_HOOK_SECRET → resolve("hook") 命中
  - 引号脱壳 ("xxx" / 'xxx')
  - 已存在的 os.environ key 不被覆盖 (shell export 优先)
  - 注释行 / 空行 / 缺 "=" 不报错
  - unknown kind → TokenNotFound (msg 含 kind)
  - 5 类 kind 全部映射正确
"""
from __future__ import annotations
import importlib
import os
import sys
from pathlib import Path

import pytest


def _fresh_resolver():
    """每个 test 重新 import token_resolver, 避开 module-level _LOADED 残留."""
    for mod in [m for m in sys.modules if m.startswith("common.token_resolver") or m == "common"]:
        sys.modules.pop(mod, None)
    return importlib.import_module("common.token_resolver")


def _write_env(content: str):
    env_path = Path(os.environ["HOME"]) / ".pre" / "env"
    env_path.write_text(content)
    # 清掉 environ 里可能残留的 PRE_*_SECRET
    for k in ("PRE_HOOK_SECRET", "PRE_NODE_SECRET", "PRE_MCP_SECRET",
              "PRE_GUI_SECRET", "PRE_OPERATOR_SECRET"):
        os.environ.pop(k, None)


def test_resolve_hook_secret_from_env_file():
    _write_env("PRE_HOOK_SECRET=hook-raw-1\n")
    tr = _fresh_resolver()
    assert tr.resolve("hook") == "hook-raw-1"


def test_missing_env_key_raises():
    _write_env("PRE_NODE_SECRET=only-node\n")
    tr = _fresh_resolver()
    with pytest.raises(tr.TokenNotFound) as excinfo:
        tr.resolve("hook")
    assert "PRE_HOOK_SECRET" in str(excinfo.value)


def test_unknown_kind_raises():
    _write_env("")
    tr = _fresh_resolver()
    with pytest.raises(tr.TokenNotFound) as excinfo:
        tr.resolve("nosuch")  # type: ignore[arg-type]
    assert "nosuch" in str(excinfo.value)


def test_quoted_value_stripped():
    _write_env('PRE_HOOK_SECRET="hook-quoted"\n')
    tr = _fresh_resolver()
    assert tr.resolve("hook") == "hook-quoted"


def test_single_quoted_value_stripped():
    _write_env("PRE_MCP_SECRET='mcp-singleq'\n")
    tr = _fresh_resolver()
    assert tr.resolve("mcp") == "mcp-singleq"


def test_environ_takes_precedence_over_env_file(monkeypatch):
    _write_env("PRE_HOOK_SECRET=from-file\n")
    monkeypatch.setenv("PRE_HOOK_SECRET", "from-environ")
    tr = _fresh_resolver()
    assert tr.resolve("hook") == "from-environ"


def test_comments_and_blank_lines_ignored():
    _write_env(
        "# header comment\n"
        "\n"
        "PRE_HOOK_SECRET=ok\n"
        "malformed-line-without-equals\n"
        "  # indented comment\n"
    )
    tr = _fresh_resolver()
    assert tr.resolve("hook") == "ok"


def test_missing_env_file_keeps_environ_value(monkeypatch):
    """env file 不存在但 shell 已 export → 仍能 resolve."""
    env_path = Path(os.environ["HOME"]) / ".pre" / "env"
    if env_path.exists():
        env_path.unlink()
    monkeypatch.setenv("PRE_GUI_SECRET", "gui-from-shell")
    tr = _fresh_resolver()
    assert tr.resolve("gui") == "gui-from-shell"


def test_all_five_kinds_map_to_correct_env_keys():
    """5 种 kind 都能找到对应 env key — 检查 _KIND_TO_ENV_KEY 表."""
    tr = _fresh_resolver()
    expected = {
        "node": "PRE_NODE_SECRET",
        "mcp": "PRE_MCP_SECRET",
        "hook": "PRE_HOOK_SECRET",
        "gui": "PRE_GUI_SECRET",
        "operator": "PRE_OPERATOR_SECRET",
    }
    assert tr._KIND_TO_ENV_KEY == expected
