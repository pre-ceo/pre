"""tools — 4 mcp tool 实现.

caller_agent_id prefix 校验 + read_pane 跨 node 严禁 嵌入.
master 端二次校验 (在 master/server.py 内).
"""
from __future__ import annotations
import json
import os
import time
from typing import Optional

from .master_client import MasterClient
from .rate_limit import get_limiter
from .audit import write_audit


def _self_node_id() -> str:
    return os.environ.get("PRE_NODE_ID", "local")


def _caller_from_agent_config(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return ""
    mcp_cfg = cfg.get("mcp") if isinstance(cfg.get("mcp"), dict) else {}
    explicit = mcp_cfg.get("caller_agent_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    node_id = os.environ.get("PRE_NODE_ID", "local")
    driver_type = cfg.get("driver_type") or cfg.get("driver") or ""
    project_name = cfg.get("project_name") or ""
    if isinstance(driver_type, str) and driver_type and isinstance(project_name, str) and project_name:
        return f"{node_id}.{driver_type}.{project_name}"
    # top-level agent_id fallback: charter-registered agent 仅塞顶层 agent_id, 没
    # mcp.caller_agent_id 也没 driver_type/project_name. 接受顶层 agent_id 为权威,
    # 但仍须以 node_id+'.' 开头 (与 _validate_caller 同约束).
    top_aid = cfg.get("agent_id")
    if isinstance(top_aid, str) and top_aid.startswith(f"{node_id}."):
        return top_aid
    return ""


def _caller_agent_id() -> str:
    """Caller agent_id 推断 (mcp client context). 简单方案: env PRE_CALLER_AGENT_ID
    or project-local pre/agent_config.json.

    Fail-fast: caller identity is security/audit critical; do not guess from cwd."""
    explicit = os.environ.get("PRE_CALLER_AGENT_ID")
    if explicit:
        return explicit
    agent_config = os.environ.get("PRE_AGENT_CONFIG")
    if agent_config:
        caller = _caller_from_agent_config(agent_config)
        if caller:
            return caller
    pwd = os.environ.get("PWD")
    if pwd:
        caller = _caller_from_agent_config(os.path.join(pwd, "pre", "agent_config.json"))
        if caller:
            return caller
    return ""


def _validate_caller(caller: str) -> tuple[bool, str]:
    """: caller_agent_id.split('.')[0] 必等 self.node_id."""
    if not caller or '.' not in caller:
        return False, "caller_format_invalid"
    if caller.split('.')[0] != _self_node_id():
        return False, f"cross_node_caller_rejected:{caller}_vs_node_{_self_node_id()}"
    return True, ""


def _check_rate_and_caller(tool_name: str) -> tuple[bool, dict, str]:
    """统一前置: rate limit + caller validate. 返 (ok, error_dict, caller)."""
    caller = _caller_agent_id()
    ok_c, reason_c = _validate_caller(caller)
    if not ok_c:
        return False, {"error": "caller_invalid", "reason": reason_c, "tool": tool_name}, caller
    limiter = get_limiter()
    ok_r, reason_r = limiter.check(caller)
    if not ok_r:
        return False, {"error": "rate_limited", "reason": reason_r, "tool": tool_name}, caller
    return True, {}, caller


def _audit(caller: str, tool: str, args_redacted: dict,
           result_status: str, latency_ms: int):
    """写 mcp audit jsonl + 推 master.db SOT."""
    write_audit({
        "ts": time.time(),
        "caller_agent_id": caller,
        "tool": tool,
        "args": args_redacted,
        "result_status": result_status,
        "latency_ms": latency_ms,
    }, node_id=_self_node_id())
    try:
        MasterClient().audit_mcp_tool_call(
            caller=caller, tool=tool, args_redacted=args_redacted,
            result_status=result_status, latency_ms=latency_ms,
        )
    except Exception:  # noqa: BLE001 — fail-safe
        pass  # audit 失败不阻 tool 返


# ---------- 4 tool 实现 ----------

def tool_send_message(to_agent: str, kind: str, payload: dict,
                       parent_id: Optional[str] = None) -> dict:
    t0 = time.time()
    ok_p, err, caller = _check_rate_and_caller("send_message")
    if not ok_p:
        _audit(caller, "send_message",
               {"to_agent": to_agent, "kind": kind, "payload_keys": list(payload.keys())},
               err.get("error", "unknown"), int((time.time() - t0) * 1000))
        return {"ok": False, **err}
    client = MasterClient()
    ok, resp = client.send_message(to_agent, kind, payload, from_agent=caller,
                                    parent_id=parent_id)
    latency_ms = int((time.time() - t0) * 1000)
    _audit(caller, "send_message",
           {"to_agent": to_agent, "kind": kind},
           "ok" if ok else "fail", latency_ms)
    return {"ok": ok, "result": resp, "latency_ms": latency_ms}


def tool_fetch_inbox(agent_id: Optional[str] = None, since: float = 0,
                      limit: int = 50, kind: Optional[str] = None) -> dict:
    t0 = time.time()
    ok_p, err, caller = _check_rate_and_caller("fetch_inbox")
    if not ok_p:
        return {"ok": False, **err}
    target = agent_id or caller  # 默认拉自己 inbox
    # : target_agent_id 必同 caller node ( 类似 read_pane 但更宽松)
    if '.' in target and target.split('.')[0] != _self_node_id():
        _audit(caller, "fetch_inbox",
               {"agent_id": target},
               "cross_node_target_rejected", int((time.time() - t0) * 1000))
        return {"ok": False, "error": "cross_node_target_rejected",
                "reason": f"caller {caller} can not fetch_inbox of {target}"}
    client = MasterClient()
    ok, resp = client.fetch_inbox(target, since=since, limit=limit, kind=kind)
    latency_ms = int((time.time() - t0) * 1000)
    _audit(caller, "fetch_inbox",
           {"agent_id": target, "since": since, "limit": limit, "kind": kind},
           "ok" if ok else "fail", latency_ms)
    return {"ok": ok, "result": resp, "latency_ms": latency_ms}


def tool_read_pane(agent_id: str, lines: int = 100,
                    grep: Optional[str] = None) -> dict:
    """: target_agent_id 必等 caller node (跨 node 严禁)."""
    t0 = time.time()
    ok_p, err, caller = _check_rate_and_caller("read_pane")
    if not ok_p:
        return {"ok": False, **err}
    if '.' in agent_id and agent_id.split('.')[0] != _self_node_id():
        _audit(caller, "read_pane",
               {"agent_id": agent_id},
               "", int((time.time() - t0) * 1000))
        return {"ok": False, "error": "",
                "reason": f"read_pane({agent_id}) from caller {caller} 跨 node 严禁 ()"}
    client = MasterClient()
    ok, resp = client.read_pane(agent_id, lines=lines, grep=grep)
    latency_ms = int((time.time() - t0) * 1000)
    _audit(caller, "read_pane",
           {"agent_id": agent_id, "lines": lines, "grep": bool(grep)},
           "ok" if ok else "fail", latency_ms)
    return {"ok": ok, "result": resp, "latency_ms": latency_ms}


def tool_cycle_state(agent_id: str) -> dict:
    t0 = time.time()
    ok_p, err, caller = _check_rate_and_caller("cycle_state")
    if not ok_p:
        return {"ok": False, **err}
    if '.' in agent_id and agent_id.split('.')[0] != _self_node_id():
        return {"ok": False, "error": "cross_node_cycle_state_rejected",
                "reason": f"caller {caller} 跨 node 查 cycle_state {agent_id} 严禁"}
    client = MasterClient()
    ok, resp = client.cycle_state(agent_id)
    latency_ms = int((time.time() - t0) * 1000)
    _audit(caller, "cycle_state", {"agent_id": agent_id},
           "ok" if ok else "fail", latency_ms)
    return {"ok": ok, "result": resp, "latency_ms": latency_ms}
