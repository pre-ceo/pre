#!/usr/bin/env python3
"""测试 cli-codex-local driver — pending parser + discover_agents.

跑法: uv run python scripts/test_cli_codex_local_driver.py

只测纯函数 (parser) 和 discover_agents (扫本机 cursor root). 不连 master, 不
触发 evaluator (集成测试在真实 node 重启后端到端做).
"""
from __future__ import annotations

import asyncio
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from drivers.cli_codex_local.driver import (  # noqa: E402
    CliCodexLocalDriver, _is_pane_busy, _has_idle_anchor,
)
from drivers.cli_codex_local.pending_parser import parse_codex_pending  # noqa: E402


# Pane fixtures — 真实 Codex TUI pattern (fixture-driven parser)
SAFE_BASH_PANE = """
Codex wants approval to run a command
command: git status
1. Yes, allow
2. No, reject
Esc to cancel
"""

DANGER_BASH_PANE = """
Codex needs permission for Bash
command: rm -rf /tmp/whatever
1. approve
2. deny
Esc to cancel
"""

EDIT_PANE = """
Do you want to approve this edit?
file: /tmp/example.txt
1. allow
2. reject
"""

UNKNOWN_PANE = """
  Press enter to confirm or esc to cancel
"""

IDLE_PANE = """
Last message
› type your prompt here
 tab to queue message       42% context left
"""

BUSY_PANE = """
• Working on the request
"""

STALE_APPROVAL_PANE = """
Codex wants approval to run a command
command: git status
1. Yes, allow
2. No, reject
... more output ...
> something else
 tab to queue message       42% context left
"""


def assert_eq(label: str, actual, expected):
    if actual != expected:
        print(f"FAIL {label}: expected {expected!r} got {actual!r}")
        return False
    print(f"PASS {label}")
    return True


def test_parser():
    print("=== parser ===")
    p = parse_codex_pending(SAFE_BASH_PANE, "test")
    ok = True
    ok &= assert_eq("safe.tool_name", p.tool_name, "Bash")
    ok &= assert_eq("safe.command", p.tool_input.get("command"), "git status")

    p = parse_codex_pending(DANGER_BASH_PANE, "test")
    ok &= assert_eq("danger.tool_name", p.tool_name, "Bash")
    ok &= assert_eq("danger.command", p.tool_input.get("command"), "rm -rf /tmp/whatever")

    p = parse_codex_pending(EDIT_PANE, "test")
    ok &= assert_eq("edit.tool_name", p.tool_name, "Edit")
    ok &= assert_eq("edit.file_path", p.tool_input.get("file_path"), "/tmp/example.txt")

    p = parse_codex_pending(UNKNOWN_PANE, "test")
    # "confirm" + "esc" 命中 weak markers → 解不出 cmd/path → 返 CodexApproval
    # generic (fail-closed, evaluator 必给 ask, driver 不会自动 allow)
    ok &= assert_eq("unknown.tool_name", p.tool_name if p else None, "CodexApproval")
    ok &= assert_eq("unknown.tool_kind", p.tool_kind if p else None, "codex_approval")

    p = parse_codex_pending(IDLE_PANE, "test")
    ok &= assert_eq("idle.is_none", p, None)

    p = parse_codex_pending(STALE_APPROVAL_PANE, "test")
    ok &= assert_eq("stale.is_none", p, None)

    return ok


def test_activity_helpers():
    print("=== activity helpers ===")
    ok = True
    ok &= assert_eq("busy_pane.is_busy", _is_pane_busy(BUSY_PANE), True)
    ok &= assert_eq("idle_pane.is_busy", _is_pane_busy(IDLE_PANE), False)
    ok &= assert_eq("idle_pane.has_idle", _has_idle_anchor(IDLE_PANE), True)
    return ok


async def test_discover():
    print("=== discover_agents ===")
    drv = CliCodexLocalDriver()
    await drv.init({"node_id": "local"})
    specs = await drv.discover_agents()
    print(f"discovered {len(specs)} codex agent(s):")
    for s in specs:
        print(f"  {s.agent_id} role={s.role} tmux={s.metadata.get('tmux_session')} cwd={s.metadata.get('cwd')}")
    # discover 不抛即 PASS (本机可能没装 codex agent)
    print(f"PASS discover (found {len(specs)})")
    return True


def main():
    results = []
    results.append(test_parser())
    results.append(test_activity_helpers())
    results.append(asyncio.run(test_discover()))
    if all(results):
        print("\nALL TESTS PASS")
        return 0
    print("\nSOME TESTS FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
