"""scripts/_token_lib.py — shared token-management helpers (library, no CLI).

被 `swap_mcp_secret_to_default.py` 和 `pre_update.py::_ensure_mcp_env_binding`
共用. Idempotent + 不打印 (caller 自己决定 UX). raw token 不进 transcript —
仅在 db rotated 但 env 写失败时, raw 通过 return dict 应急返回供 caller 处理.
"""
from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

# 复用 master lib (核心 stdlib only, CLAUDE.md 硬约束 OK)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PRE_ROOT = os.path.dirname(_HERE)
if os.path.join(_PRE_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_PRE_ROOT, "src"))

from master.persistence import MasterDB  # noqa: E402
from master.auth import hash_token        # noqa: E402

DEFAULT_DB_PATH = str(Path.home() / ".pre" / "data" / "master.db")
DEFAULT_ENV_PATH = str(Path.home() / ".pre" / "env")


def _extract_env_var(env_path: Path, key: str) -> Optional[str]:
    """读 ~/.pre/env 格式文件, 找 KEY=value 行, 返 value (剥引号). 找不到返 None.
    跳过 # 注释 / 空行 / 末行无 \\n. 不修文件."""
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k.strip() != key:
                continue
            # 去末行 inline 注释 `# synced from ...`
            v = v.split("#", 1)[0].strip()
            v = v.strip().strip('"').strip("'")
            return v or None
    except OSError:
        return None
    return None


def _rewrite_env_atomically(env_path: Path, key: str, new_value: str) -> str:
    """原子改 KEY= 行. 找不到 → 追加. 返 'replaced' | 'appended'.
    保 umask 077 + chmod 600 防止泄漏. tmp 失败可通过 tmp.unlink() 清理."""
    if not env_path.exists():
        raise OSError(f"env file not found: {env_path}")
    src = env_path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    replaced = False
    new_lines = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            tail = "\n" if line.endswith("\n") else ""
            # 保留同行 inline 注释 (e.g. " # synced from ...") 的位置 — 删之
            # (轮换后重写更干净, 不保留旧 inline note)
            new_lines.append(f"{key}={new_value}{tail}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"{key}={new_value}\n")
        marker = "appended"
    else:
        marker = "replaced"
    tmp = env_path.with_suffix(env_path.suffix + f".tmp.{int(time.time())}")
    try:
        old_umask = os.umask(0o077)
        try:
            tmp.write_text("".join(new_lines), encoding="utf-8")
        finally:
            os.umask(old_umask)
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return marker


def ensure_mcp_env_uses_node_prefix(
    db_path: str = DEFAULT_DB_PATH,
    env_path: str = DEFAULT_ENV_PATH,
) -> dict:
    """Idempotent. 让 `~/.pre/env::PRE_MCP_SECRET` 绑定到 node-prefix mcp token
    (e.g. mcp-default agent_id='local'), 不再是严格 agent_id 绑定 (含 '.' 的).

    严格绑定 (`local.cli-claude-code-local.pre`) 在 sibling repo MCP shim 修复后
    (8e8475c PRE_CALLER_CWD plumb) 会导致 master `mcp_from_agent_mismatch` 拒.
    node-prefix (`local`) 接受任何 `local.*` caller, 是多 sibling 场景的正确选择.

    Returns dict, status:
      - "ok"      no-op. reason ∈ {already_node_prefix, no_env_file,
                  no_secret_in_env, token_not_found_in_db, token_revoked_in_db}
      - "swapped" 已 rotate + env 已更新. 含 old_label / new_label /
                  old_bound_agent_id / env_marker.
      - "error"   reason 描述 (e.g. no_active_node_prefix_mcp_token,
                  insert_bus_token_failed, env_rewrite_failed). 若 db 已 rotate
                  但 env 写失败, 返 raw_emergency 让 caller 应急写.

    本函数 **不打印** — caller 决定 UX. raw 不放 stdout / logger / return
    (except raw_emergency on env_rewrite_failed).
    """
    env_p = Path(env_path)

    if not env_p.exists():
        return {"status": "ok", "reason": "no_env_file", "env_path": str(env_p)}

    secret = _extract_env_var(env_p, "PRE_MCP_SECRET")
    if not secret:
        return {"status": "ok", "reason": "no_secret_in_env"}

    if not os.path.exists(db_path):
        return {"status": "ok", "reason": "db_not_found", "db_path": db_path}

    db = MasterDB(db_path)
    h = hash_token(secret)
    row = db.get_bus_token_by_hash(h)
    if not row:
        return {"status": "ok", "reason": "token_not_found_in_db"}
    if row.get("revoked_ts"):
        return {"status": "ok", "reason": "token_revoked_in_db",
                "old_label": row.get("label")}
    if row.get("role") != "mcp":
        return {"status": "ok", "reason": "not_mcp_role",
                "role": row.get("role")}

    agent_id = row.get("agent_id") or ""
    if "." not in agent_id:
        return {"status": "ok", "reason": "already_node_prefix",
                "bound_agent_id": agent_id, "label": row.get("label")}

    # 需 swap. 找 active mcp token agent_id 不含 '.' (node prefix 绑定).
    candidates = [
        r for r in db.list_bus_tokens(include_revoked=False)
        if r.get("role") == "mcp"
        and r.get("agent_id")
        and "." not in r["agent_id"]
        and not r.get("revoked_ts")
    ]
    if not candidates:
        return {"status": "error", "reason": "no_active_node_prefix_mcp_token",
                "hint": ("python3 scripts/pre_token.py issue --role mcp "
                         "--label mcp-default --agent-id local")}
    target = next(
        (r for r in candidates if "mcp-default" in (r.get("label") or "")),
        candidates[0],
    )
    old_label = target["label"]

    # rotate target: revoke + issue new (label 加 ts+rand 后缀防 UNIQUE 冲突)
    db.revoke_bus_token(old_label)
    raw_new = secrets.token_hex(16)
    new_label = f"mcp-default.{int(time.time())}.{secrets.token_hex(3)}"
    h_new = hash_token(raw_new)
    insert_ok = db.insert_bus_token(
        h_new, new_label, target["role"], target["scopes"],
        agent_id=target.get("agent_id"),
        expires_ts=target.get("expires_ts"),
        metadata={
            "rotated_from": old_label,
            "rotated_ts": time.time(),
            "rotated_by": "_token_lib.ensure_mcp_env_uses_node_prefix",
        },
    )
    if not insert_ok:
        return {"status": "error", "reason": "insert_bus_token_failed",
                "new_label_attempted": new_label}

    # 写 env
    try:
        marker = _rewrite_env_atomically(env_p, "PRE_MCP_SECRET", raw_new)
    except OSError as e:
        return {"status": "error", "reason": "env_rewrite_failed",
                "error_detail": str(e)[:200],
                "new_label": new_label,
                "raw_emergency": raw_new}

    return {
        "status": "swapped",
        "old_label": old_label,
        "old_bound_agent_id": agent_id,
        "new_label": new_label,
        "new_bound_agent_id": target.get("agent_id"),
        "env_marker": marker,
    }
