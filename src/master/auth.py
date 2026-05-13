"""
auth — multi-token RBAC: 校验 Bearer + role/scope 检查.

设计:
  - master 持 sha256 hash, 不持 raw
  - 每 token 一行 bus_tokens 表 row (label/role/scopes/agent_id/timestamps)
  - role-based scope 检查 (第一期粗颗): 4 个 role 各配默认 scopes
  - mcp token 的 agent_id 字段两种语义:
      含 '.' → 完整 agent_id, from_agent 必精确相等 (跨 node deploy-time 严格绑定)
      不含 '.' → node prefix, from_agent 必以 "<prefix>." 开头
                  (本机一 token 服务同 node 多 agent; 仍堵跨 node 冒充)
      为空    → 拒所有携带 from_agent 的请求 (e.g. send_message)

Fail-closed: 任何异常 → 拒. 不留 legacy `--secret` env 后门.

详细决策见 dev-workflow/features/260510-multi-token-rbac-create.md
"""
from __future__ import annotations
import hashlib
from typing import Optional


# ----- Role → 默认 scopes -----
# 来源差异化: 每个 role 对应 master HTTP 的一类 caller 来源, PR2 会按
# (role, path, source IP) 三元组在 server.py 端做白名单校验.
ROLE_DEFAULT_SCOPES: dict[str, set[str]] = {
    # node ws daemon (src/node/client.py ↔ master /node ws + /files)
    "node": {"bus.connect", "bus.message.send", "bus.message.fetch"},
    # 运维 / 你本人 (全权限, 不与 gui 混)
    "operator": {"admin.*", "agent.*", "bus.*"},
    # 轻量 CLI (legacy; PR4 后这条收缩, agent 不准用)
    "cli": {"bus.message.send", "bus.message.fetch"},
    # pre_mcp 子进程: 4 个 tool 对应 scope; 必须带 agent_id binding
    "mcp": {
        "bus.message.send", "bus.message.fetch",
        "bus.pane.read", "bus.cycle_state",
    },
    # pre_ui browser → master HTTP(S). master /auth/init 颁发后存 browser.
    # 暂与 operator 同 scope; PR2 在 path 层用 role 区分来源, 不靠 scope.
    "gui": {"admin.*", "agent.*", "bus.*"},
    # hook / runtime 模块 (cycle_alert / runtime/* / remote_node_manager 等)
    # 跑在 hook 子进程, 走本机 loopback 调 master HTTP.
    "hook": {"bus.message.send", "bus.message.fetch", "agent.read"},
}

# 已知 role 集合 (其它 role 一律拒)
KNOWN_ROLES = set(ROLE_DEFAULT_SCOPES.keys())


def hash_token(raw: str) -> str:
    """sha256 hex. 用于 DB 查询 + 文件存储."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def has_scope(token_scopes: list, required: str) -> bool:
    """检查 token_scopes (DB 存的 list) 是否覆盖 required scope.

    支持 wildcard 匹配:
      "bus.*" 覆盖 "bus.message.send" / "bus.connect" 等
      "admin.*" 覆盖 "admin.tokens" 等
      "*" 全覆盖

    精确匹配也支持: "bus.message.send" in token_scopes → True.
    """
    if not required:
        return True
    if "*" in token_scopes:
        return True
    if required in token_scopes:
        return True
    # wildcard prefix: "bus.*" 覆盖 "bus.xxx"
    parts = required.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i]) + ".*"
        if prefix in token_scopes:
            return True
    return False


def verify_token(db, raw_bearer: str, required_scope: str = "",
                 expected_role: Optional[str] = None,
                 from_agent: Optional[str] = None) -> tuple[bool, str, dict]:
    """校验 Bearer + scope + agent_id 锁定.

    Args:
      db: MasterDB 实例
      raw_bearer: 客户端送来的 Bearer 字符串
      required_scope: endpoint 要求的 scope (e.g. "bus.message.send")
                       空字符串 = 仅校验 token 有效, 不查 scope
      expected_role: 限定 role (e.g. ws /node 路径仅接 role=node)
                       None = 任何 role 都行 (前提是 scope 匹配)
      from_agent: 若非 None, 检查 mcp token 绑定的 agent_id 必相等
                       用于 POST /send 防伪装

    Returns:
      (ok, reason, ctx)
        ok: True/False
        reason: 失败时的错误码 (供 audit log)
        ctx: 命中时的 token 上下文 dict {label, role, scopes, agent_id}
              失败时为 {}
    """
    if not raw_bearer:
        return False, "missing_bearer", {}
    try:
        h = hash_token(raw_bearer)
    except (UnicodeError, AttributeError):
        return False, "bad_bearer_format", {}

    try:
        entry = db.get_bus_token_by_hash(h)
    except Exception:  # noqa: BLE001 — fail-closed
        return False, "db_error", {}

    if not entry:
        return False, "token_unknown", {}

    # 撤销
    if entry.get("revoked_ts"):
        return False, "token_revoked", {}

    # 过期
    import time as _time
    exp = entry.get("expires_ts")
    if exp and exp < _time.time():
        return False, "token_expired", {}

    role = entry.get("role", "")
    if role not in KNOWN_ROLES:
        return False, f"unknown_role:{role}", {}

    # 限定 role (e.g. ws /node 仅 node role)
    if expected_role and role != expected_role:
        return False, f"role_mismatch:expected_{expected_role}_got_{role}", {}

    # scope 检查
    if required_scope and not has_scope(entry.get("scopes", []), required_scope):
        return False, f"scope_denied:{required_scope}", {}

    # mcp token 身份锁定 (两种 binding 语义见模块 docstring)
    if from_agent is not None and role == "mcp":
        bound = entry.get("agent_id") or ""
        if not bound:
            return False, "mcp_token_missing_agent_id_binding", {}
        if '.' in bound:
            # 完整 agent_id binding: 严格相等
            if from_agent != bound:
                return False, f"mcp_from_agent_mismatch:{from_agent}_vs_{bound}", {}
        else:
            # node prefix binding: from_agent 必以 "<prefix>." 开头
            if not from_agent.startswith(f"{bound}."):
                return False, f"mcp_from_agent_prefix_mismatch:{from_agent}_vs_{bound}.*", {}

    # 命中 → 顺便 touch last_used_ts (容错)
    try:
        db.touch_bus_token(h)
    except Exception:  # noqa: BLE001
        pass

    ctx = {
        "label": entry.get("label"),
        "role": role,
        "scopes": entry.get("scopes", []),
        "agent_id": entry.get("agent_id"),
    }
    return True, "ok", ctx


def extract_bearer(authorization_header: str) -> str:
    """从 'Authorization: Bearer xxx' 提 raw token. 找不到返空字符串."""
    if not authorization_header:
        return ""
    parts = authorization_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1]
