#!/usr/bin/env python3
"""scripts/swap_mcp_secret_to_default.py — CLI 入口, 实际逻辑在 _token_lib.py.

修 ~/.pre/env::PRE_MCP_SECRET 绑死单个 agent_id 的问题 (sibling MCP shim 修后
触发 mcp_from_agent_mismatch). 本脚本 idempotent + 不打 raw / sha 到 transcript.

直接跑:  python3 scripts/swap_mcp_secret_to_default.py
集成路径: `pre update` 已含同一 step (走 scripts/pre_update.py).
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _token_lib import ensure_mcp_env_uses_node_prefix  # noqa: E402

C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"


def main() -> int:
    print(f"{C_MAGENTA}━━━ swap PRE_MCP_SECRET → node-prefix mcp-default ━━━{C_RESET}")
    result = ensure_mcp_env_uses_node_prefix()
    status = result.get("status")

    if status == "ok":
        reason = result.get("reason", "?")
        print(f"  {C_DIM}[no-op]{C_RESET}  {reason}")
        if reason == "already_node_prefix":
            print(f"  {C_DIM}env::PRE_MCP_SECRET 已绑 node prefix "
                  f"(agent_id='{result.get('bound_agent_id')}', "
                  f"label='{result.get('label')}'), 无需 swap.{C_RESET}")
        return 0

    if status == "swapped":
        print(f"  {C_CYAN}[revoked]{C_RESET}  {result.get('old_label')} "
              f"(was bound to {C_DIM}{result.get('old_bound_agent_id')}{C_RESET})")
        print(f"  {C_CYAN}[issued ]{C_RESET}  {result.get('new_label')} "
              f"(now bound to {C_DIM}{result.get('new_bound_agent_id')}{C_RESET})")
        print(f"  {C_CYAN}[env    ]{C_RESET}  ~/.pre/env PRE_MCP_SECRET "
              f"{result.get('env_marker')} (mode 600)")
        print()
        print(f"{C_MAGENTA}━━━ done ━━━{C_RESET}")
        print(f"  raw token {C_DIM}已写入 ~/.pre/env, 未打印 (避免 transcript 泄漏){C_RESET}")
        print()
        print(f"{C_CYAN}下一步{C_RESET}: bus daemon + sibling claude code 需重启读新 env")
        print(f"  {C_DIM}pre bus restart                                                  # daemon{C_RESET}")
        print(f"  {C_DIM}tmux kill-session -t <sibling>; pre spawn <agent_id>             # 每个 sibling 用到时再做{C_RESET}")
        return 0

    # error
    reason = result.get("reason", "?")
    print(f"  {C_YELLOW}[error]{C_RESET}  {reason}", file=sys.stderr)
    if "hint" in result:
        print(f"  {C_DIM}{result['hint']}{C_RESET}", file=sys.stderr)
    if reason == "env_rewrite_failed":
        # db 已 rotated, env 写失败 — 应急 raw 必须输出让 user 手动写回
        print(f"  {C_YELLOW}db 已 rotated, env 写失败. 手动写一行到 ~/.pre/env:"
              f"{C_RESET}", file=sys.stderr)
        print(f"    PRE_MCP_SECRET={result['raw_emergency']}")
    return 3


if __name__ == "__main__":
    sys.exit(main())
