"""token_resolver — 单点 token 出入口.

所有 hook / runtime / master / scripts 调 master HTTP 时, 通过本模块按
caller kind 取 Bearer token. 替代旧的 ``os.environ.get("PRE_SECRET", "pre")``
fallback (静默 401 难排查).

唯一真实源: ``~/.pre/env`` (chmod 600, 不入 git). schema:

    PRE_NODE_SECRET=<node-default raw>      # src/node/client.py
    PRE_MCP_SECRET=<mcp-bound raw>           # pre_mcp 子进程 (本模块外读)
    PRE_HOOK_SECRET=<hook-default raw>       # hook / runtime / 人工 CLI
    PRE_GUI_SECRET=<gui-default raw>         # master 颁发给 browser 的引导

pre_mcp 子进程**不**走本模块 (CLAUDE.md 硬约束: pre_mcp 不引 src/). 它在
``pre_mcp/__main__.py`` 里有自己的 ``_load_env_file``, 与本模块行为对齐
(KEY=VALUE 行 / 不覆盖已存在 environ / 引号脱壳).
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Literal


_ENV_FILE = Path.home() / ".pre" / "env"
_LOADED = False


def _load_env_file(path: Path = _ENV_FILE) -> None:
    """加载 ~/.pre/env 到 os.environ. 已存在的 environ key 不覆盖
    (让 shell 显式 export 仍能 override). 失败 silent (文件不在 / 格式错).
    """
    global _LOADED
    try:
        if not path.is_file():
            _LOADED = True
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                continue
            k, _, v = s.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass
    _LOADED = True


_KIND_TO_ENV_KEY: dict[str, str] = {
    "node":     "PRE_NODE_SECRET",
    "mcp":      "PRE_MCP_SECRET",
    "hook":     "PRE_HOOK_SECRET",
    "gui":      "PRE_GUI_SECRET",
    "operator": "PRE_OPERATOR_SECRET",  # admin/scope=admin.* 给 GUI/CLI 运维, 不给 agent
}


class TokenNotFound(RuntimeError):
    """~/.pre/env 缺对应 key, 或 kind 不识别. fail-fast 取代旧的 'pre' 默认."""


def resolve(kind: Literal["node", "mcp", "hook", "gui", "operator"]) -> str:
    """按 caller kind 取 raw bearer. 找不到 raise TokenNotFound.

    扩展新 kind: 在本文件 _KIND_TO_ENV_KEY 加映射即可, 同时
    src/master/auth.py:ROLE_DEFAULT_SCOPES 加对应 role,
    scripts/start_master.py:_bootstrap_tokens 加 default token.
    """
    if not _LOADED:
        _load_env_file()
    env_key = _KIND_TO_ENV_KEY.get(kind)
    if not env_key:
        raise TokenNotFound(f"unknown caller kind: {kind!r}")
    val = os.environ.get(env_key)
    if not val:
        raise TokenNotFound(
            f"~/.pre/env missing {env_key} (kind={kind}). "
            f"issue via `python3 scripts/pre_token.py issue --role <role>` "
            f"and append to ~/.pre/env"
        )
    return val


# Eager load ~/.pre/env at import time — let module-level callers
# (src/config.py:RULE_ROOT, src/master/server.py path constants 等) see
# install.sh-written PRE_RULE_ROOT/PRE_LOG_DIR without explicit resolve() trigger.
# idempotent (k not in os.environ check inside _load_env_file).
_load_env_file()
