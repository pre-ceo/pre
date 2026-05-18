"""claude driver audit — PreToolUse 决策 append-only jsonl.

写 $PRE_LOG_DIR/claude_driver/auto_decision_{YYYYMMDD}.jsonl. schema 对齐 codex /
gemini driver._audit (audit_view.py KINDS["driver_decision"]):
  ts, agent_id, tmux_session, tool_name, tool_input_preview,
  decision, reason, source, action, ok
"driver" 字段不写, audit_view 从目录名 "claude_driver" 衍生 → "claude".
cwd 不写 (含 home path, audit_view fields 白名单已排除).

claude 走 PreToolUse hook 路径 (本模块), 跟 codex/gemini 走 driver 内嵌 evaluator
+ pane scrape 不同, 但出口 schema 相同, 给 audit_view 统一读.

Fail-safe: 任何异常吞掉, audit 不能阻断 hook 决策.
HC-PRE-1 stdlib only.
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone


def resolve_agent_from_cwd(cwd: str) -> tuple[str, str]:
    """从 cwd/pre/agent_config.json 反查 (agent_id, tmux_session).

    优先级跟 pre_mcp/tools.py:_caller_from_agent_config 对齐:
      1. mcp.caller_agent_id (显式)
      2. driver_type + project_name → "{node}.{driver_type}.{project}"
      3. 顶层 agent_id (charter-registered)
    任一失败返 ("", "")."""
    if not cwd:
        return "", ""
    try:
        with open(os.path.join(cwd, "pre", "agent_config.json"),
                  encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "", ""
    if not isinstance(cfg, dict):
        return "", ""
    node_id = os.environ.get("PRE_NODE_ID", "local")
    aid = ""
    mcp_cfg = cfg.get("mcp")
    if isinstance(mcp_cfg, dict):
        explicit = mcp_cfg.get("caller_agent_id")
        if isinstance(explicit, str) and explicit:
            aid = explicit
    if not aid:
        driver_type = cfg.get("driver_type") or cfg.get("driver") or ""
        project_name = cfg.get("project_name") or ""
        if (isinstance(driver_type, str) and driver_type
                and isinstance(project_name, str) and project_name):
            aid = f"{node_id}.{driver_type}.{project_name}"
    if not aid:
        top = cfg.get("agent_id")
        if isinstance(top, str) and top.startswith(f"{node_id}."):
            aid = top
    tmux = cfg.get("tmux_session") or ""
    if not isinstance(tmux, str):
        tmux = ""
    return aid, tmux


def tool_preview(tool_name: str, tool_input: dict) -> str:
    """单 string preview, 上限 240 字符 (跟 codex driver _preview_tool_input 对齐)."""
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))[:240]
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        return str(tool_input.get("file_path", ""))[:240]
    if tool_name in ("Grep", "Glob"):
        return str(tool_input.get("pattern", ""))[:240]
    if tool_name in ("WebFetch", "WebSearch"):
        return str(tool_input.get("url") or tool_input.get("query", ""))[:240]
    try:
        return json.dumps(tool_input, ensure_ascii=False)[:240]
    except (TypeError, ValueError):
        return ""


def _audit_path() -> str:
    """$PRE_LOG_DIR/claude_driver/auto_decision_{YYYYMMDD}.jsonl.
    PRE_LOG_DIR env-first, sibling fallback (../pre_log)."""
    here = os.path.dirname(os.path.abspath(__file__))  # pre/src
    pre_root = os.environ.get("PRE_ROOT") or os.path.dirname(here)
    log_root = os.environ.get("PRE_LOG_DIR") or os.path.normpath(
        os.path.join(pre_root, "..", "pre_log"))
    audit_dir = os.path.join(log_root, "claude_driver")
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return os.path.join(audit_dir, f"auto_decision_{date}.jsonl")


def audit_decision(input_data: dict, result: dict,
                   decision: str, reason: str) -> None:
    """append 一条 jsonl. fail-safe — audit 失败不能阻断 hook 决策."""
    try:
        cwd = str(input_data.get("cwd") or "")
        tool_name = str(input_data.get("tool_name") or "")
        tool_input = input_data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {"value": tool_input}
        agent_id, tmux_session = resolve_agent_from_cwd(cwd)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "tmux_session": tmux_session,
            "tool_name": tool_name,
            "tool_input_preview": tool_preview(tool_name, tool_input),
            "decision": decision,
            "reason": reason,
            "source": str(result.get("source") or ""),
            "action": "hook_decision",
            "ok": True,
        }
        path = _audit_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass
