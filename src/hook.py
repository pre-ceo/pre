"""
pre 核心 hook 入口 (CLI shell)
stdin (JSON) → evaluate_prehook → stdout (Claude Code hookSpecificOutput JSON)

决策逻辑在 src/prehook_evaluator.py, 本模块只剩:
  - stdin 解析 + fail-safe 兜底
  - transcript_path 副作用持久化 (供 stop hook)
  - 调 evaluator
  - hookSpecificOutput JSON 包装

I/O 契约遵循 Claude Code PreToolUse 官方规范:
  Input:  { tool_name, tool_input, session_id, cwd, permission_mode, ... }
  Output: { hookSpecificOutput: { hookEventName, permissionDecision, permissionDecisionReason } }

(codex / gemini 走 driver 内嵌 evaluator + pane scrape, 不调本 hook.)
"""
import sys
import os
import json

from .config import load_config
from .prehook_evaluator import evaluate_prehook


def main():
    """PreToolUse hook 主入口."""
    cfg = load_config()

    # --- 1. 解析 stdin ---
    try:
        input_data = json.load(sys.stdin)
    except Exception as e:
        # Fail-safe: 解析失败一律降级 ask, 绝不默认放行
        return output("ask", f"stdin parse failed: {e}")

    cwd = input_data.get("cwd", "")
    transcript_path = input_data.get("transcript_path", "")

    # 保存 transcript_path 供 stop hook 使用 (stop hook 自身收不到这个字段)
    if transcript_path and cwd and cfg.mode == "enforce":
        _save_transcript_path(cfg.pre_base_dir, cwd, transcript_path)

    # --- 2. 评估 ---
    try:
        result = evaluate_prehook(input_data)
    except Exception as e:
        # fail-closed: evaluator 异常 → ask
        return output("ask", f"evaluator raised: {e}")

    return output(result.get("decision", "ask"), result.get("reason", ""))


def output(decision: str, reason: str):
    """输出符合 Claude Code PreToolUse 契约的 JSON.
    decision: "allow" | "deny" | "ask"
    """
    result = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        result["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(result))
    sys.exit(0)


def _save_transcript_path(pre_base_dir: str, cwd: str, transcript_path: str):
    """保存 transcript_path 到 agent 目录, 供 stop hook 读取"""
    from .governor import ensure_agent_dir
    agent_dir = ensure_agent_dir(pre_base_dir, cwd)
    path_file = os.path.join(agent_dir, "transcript_path.txt")
    try:
        with open(path_file, "w") as f:
            f.write(transcript_path)
    except OSError:
        pass
