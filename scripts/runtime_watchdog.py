"""
runtime_watchdog — fn_runtime crash watchdog 兜底 (cron 60s 入口).

, , .
0 LLM cost: 全 syscall (tmux has-session / kill -0 / socket connect_ex).
event-driven 优先 (tmux session-closed hook), 本脚本是 cron 兜底.
HC-A9/G10 polling 禁止 — 单次跑完退出, 不 sleep+repeat loop.

用法:
  uv run python scripts/runtime_watchdog.py
  (master 内嵌 cron 60s 间隔触发)
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

# 路径接入
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_HERE, "src"))

from runtime.process_lifecycle import scan_crashes, list_targets


def main():
    enabled = list_targets(only_enabled=True)
    if not enabled:
        print("[watchdog] no enabled target, noop")
        return 0
    crashes = scan_crashes(initiated_by="watchdog_cron")
    print(f"[watchdog] checked {len(enabled)} targets, "
          f"{len(crashes)} crashed: "
          f"{[c['target_id'] for c in crashes]}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
