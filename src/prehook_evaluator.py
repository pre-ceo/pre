"""pre PreToolUse evaluator — importable, 跨 caller 复用.

抽自 src/hook.py. hook.py 保留 stdin/stdout CLI 行为, 改成调本模块.
其他 caller (例 cli-codex-local driver) 直接 import evaluate_prehook 用.

输入兼容 Claude Code PreToolUse shape:
  tool_name, tool_input, session_id, cwd, transcript_path, permission_mode

输出 driver-friendly:
  {
    "decision": "allow" | "ask" | "deny",
    "reason": str,
    "source": "observe" | "local" | "cache" | "governor" | "governor_no_cache" | "fallback",
    "agent_pre_dir": str | None,
  }

Fail-safe: 任何上游异常未捕获 → 调用方负责 fail-closed (返 ask).
本模块自身不抛 — config/log 异常被 try/except 吞.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .config import load_config
from .logger import log_event
from .rules import evaluate as local_evaluate, GOVERNOR_NO_CACHE
from .cache import cache_key, get_cached, set_cached
from .governor import query_governor, ensure_agent_dir
from .analyzer import load_agent_config


def _safe_preview(tool_name: str, tool_input: dict) -> dict:
    """构建脱敏 log preview."""
    if tool_name == "Bash":
        return {"command_preview": str(tool_input.get("command", ""))[:200]}
    if tool_name in ("Read", "Write", "Edit"):
        return {"file_path": tool_input.get("file_path", "")}
    if tool_name in ("Grep", "Glob"):
        return {"pattern": tool_input.get("pattern", "")}
    if tool_name == "Agent":
        return {"description": tool_input.get("description", "")}
    return {}


def evaluate_prehook(
    input_data: dict[str, Any],
    *,
    log: bool = True,
) -> dict[str, Any]:
    """主入口. input_data 兼容 Claude Code PreToolUse hook stdin shape.

    log=False 时跳过 log_event (driver 内嵌调用通常自带 audit 不重复)."""
    cfg = load_config()

    tool_name = str(input_data.get("tool_name") or "unknown")
    raw_tool_input = input_data.get("tool_input") or {}
    if not isinstance(raw_tool_input, dict):
        tool_input: dict = {"value": raw_tool_input}
    else:
        tool_input = raw_tool_input
    session_id = str(input_data.get("session_id") or "unknown")
    cwd = str(input_data.get("cwd") or "")
    transcript_path = str(input_data.get("transcript_path") or "")

    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "mode": cfg.mode,
        "cwd": cwd,
        "session": session_id[:12],
    }
    if cfg.verbose:
        entry["input"] = tool_input
    else:
        entry.update(_safe_preview(tool_name, tool_input))

    def _finish(decision: str, reason: str, source: str,
                agent_pre_dir: str | None = None) -> dict:
        entry["decision"] = decision
        entry["reason"] = reason
        entry["source"] = source
        if agent_pre_dir:
            entry["agent_dir"] = agent_pre_dir
        if log:
            try:
                log_event(cfg.log_dir, entry)
            except Exception:
                pass  # log 失败不能阻断决策
        return {
            "decision": decision,
            "reason": reason,
            "source": source,
            "agent_pre_dir": agent_pre_dir,
        }

    # observe 模式: 一律 ask, 不进决策链
    if cfg.mode == "observe":
        return _finish("ask", "", "observe")

    if cfg.mode != "enforce":
        return _finish("ask", "", "fallback")

    agent_pre_dir = ensure_agent_dir(cfg.pre_base_dir, cwd)

    # --- 3a. 本地规则 (零延迟) ---
    decision, reason = local_evaluate(tool_name, tool_input, cwd)
    skip_cache = (decision == GOVERNOR_NO_CACHE)

    if decision is not None and decision != GOVERNOR_NO_CACHE:
        agent_config = load_agent_config(agent_pre_dir, cwd)
        agent_mode = agent_config.get("mode", "supervised")
        if decision == "ask" and agent_mode in ("autonomous", "freerun"):
            decision = "deny"
            reason = f"[auto-deny] {reason}"
        return _finish(decision, reason, "local", agent_pre_dir)

    # --- 3b. 缓存 ---
    if not skip_cache:
        ck = cache_key(tool_name, tool_input)
        cached = get_cached(agent_pre_dir, ck, ttl=3600)
        if cached is not None:
            c_decision, c_reason = cached
            return _finish(c_decision, c_reason, "cache", agent_pre_dir)

    # --- 3c. Governor ---
    decision, reason = query_governor(
        tool_name=tool_name,
        tool_input=tool_input,
        session_id=session_id,
        cwd=cwd,
        agent_pre_dir=agent_pre_dir,
        rules_dir=cfg.rules_dir,
        timeout=cfg.governor_timeout,
        transcript_path=transcript_path,
        provider=cfg.governor_provider,
    )
    agent_config = load_agent_config(agent_pre_dir, cwd)
    agent_mode = agent_config.get("mode", "supervised")
    if decision == "ask" and agent_mode in ("autonomous", "freerun"):
        decision = "deny"
        reason = f"[auto-deny] {reason}"
    if not skip_cache:
        set_cached(agent_pre_dir, cache_key(tool_name, tool_input), decision, reason)
    source = "governor_no_cache" if skip_cache else "governor"
    return _finish(decision, reason, source, agent_pre_dir)
