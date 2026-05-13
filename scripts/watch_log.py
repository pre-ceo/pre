#!/usr/bin/env python3
"""
pre 日志实时监控脚本
用法:
  uv run python scripts/watch_log.py # 实时跟踪今日日志
  uv run python scripts/watch_log.py --dump # 一次性输出今日全部日志
  uv run python scripts/watch_log.py --stats # 统计汇总
"""
import sys
import os
import json
import time
import argparse
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")


def get_today_log():
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"pre_hook_{date_str}.jsonl")


def format_entry(line: str) -> str:
    """将 JSONL 行格式化为可读输出"""
    try:
        e = json.loads(line)
    except Exception:
        return line.strip()

    ts = e.get("ts", "")[:19].replace("T", " ")  # 截断到秒
    tool = e.get("tool", "?")
    decision = e.get("decision", "?")
    mode = e.get("mode", "?")

    # 工具特征摘要 (Claude Code + Gemini CLI 通用)
    detail = ""
    inp = e.get("input", {})
    if tool in ("Bash", "run_shell_command"):
        cmd = inp.get("command", e.get("command_preview", ""))
        detail = cmd[:80]
    elif tool in ("Read", "Write", "Edit", "read_file", "write_file", "replace"):
        fp = (inp.get("file_path") or inp.get("absolute_path")
              or e.get("file_path", ""))
        detail = fp
    elif tool in ("Grep", "Glob", "grep_search", "glob", "list_directory"):
        pattern = inp.get("pattern", e.get("pattern", ""))
        path = inp.get("path") or inp.get("dir_path") or inp.get("absolute_path", "")
        detail = f"{pattern} {path}".strip() if pattern else path
    elif tool == "Agent":
        detail = inp.get("description", e.get("description", ""))
    elif tool == "WebSearch":
        detail = inp.get("query", "")
    elif tool == "WebFetch":
        detail = inp.get("url", "")
    else:
        # 其他工具: 取 input 的第一个有意义字段
        if inp:
            for key in ("command", "query", "url", "description", "prompt",
                        "file_path", "absolute_path", "path", "pattern", "text"):
                if inp.get(key):
                    detail = f"{key}={str(inp[key])[:60]}"
                    break
            if not detail:
                first_key = next(iter(inp))
                val = str(inp[first_key])
                detail = f"{first_key}={val[:60]}"

    # 决策标记
    tag = {"allow": "[ALLOW]", "deny": "[DENY ]", "ask": "[ ASK ]"}.get(decision, f"[{decision:^5s}]")

    # 决策来源
    source = e.get("source", "")
    source_tag = f"({source})" if source else ""

    line_out = f"{ts}  {tag}  {tool:<12s}  {source_tag:<12s}{detail}"

    # 附加 reason (governor/governor_no_cache/local ask 等都可能带 reason)
    reason = e.get("reason", "")
    if reason:
        line_out += f"\n{'':>20s}  >> {reason[:120]}"

    return line_out


def watch(log_path: str):
    """实时跟踪日志 (类似 tail -f)"""
    print(f"-- Watching: {log_path}")
    print(f"-- Press Ctrl+C to stop\n")

    # 如果文件不存在, 等待它被创建
    while not os.path.exists(log_path):
        print(f"-- Waiting for log file to be created...")
        time.sleep(2)

    with open(log_path, "r") as f:
        # 先输出已有内容
        for line in f:
            if line.strip():
                print(format_entry(line))
        # 持续跟踪新内容
        while True:
            line = f.readline()
            if line.strip():
                print(format_entry(line))
            else:
                time.sleep(0.5)


def dump(log_path: str):
    """一次性输出全部日志"""
    if not os.path.exists(log_path):
        print(f"-- No log file: {log_path}")
        return
    with open(log_path, "r") as f:
        for line in f:
            if line.strip():
                print(format_entry(line))


def stats(log_path: str):
    """统计汇总"""
    if not os.path.exists(log_path):
        print(f"-- No log file: {log_path}")
        return

    from collections import Counter
    tool_counts = Counter()
    decision_counts = Counter()
    source_counts = Counter()
    total = 0

    with open(log_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                tool_counts[e.get("tool", "?")] += 1
                decision_counts[e.get("decision", "?")] += 1
                source_counts[e.get("source", "n/a")] += 1
                total += 1
            except Exception:
                pass

    print(f"-- Log: {log_path}")
    print(f"-- Total events: {total}\n")

    print("Tool Calls:")
    # Tool = 工具名, Count = 调用次数
    print(f"  {'Tool':<16s} {'Count':>6s}")
    print(f"  {'----':<16s} {'-----':>6s}")
    for tool, count in tool_counts.most_common():
        print(f"  {tool:<16s} {count:>6d}")

    print(f"\nDecisions:")
    # Decision = 决策类型 (allow/ask/deny)
    print(f"  {'Decision':<10s} {'Count':>6s}")
    print(f"  {'--------':<10s} {'-----':>6s}")
    for dec, count in decision_counts.most_common():
        print(f"  {dec:<10s} {count:>6d}")

    print(f"\nSources:")
    # Source = 决策来源 (local/cache/governor)
    print(f"  {'Source':<12s} {'Count':>6s}")
    print(f"  {'------':<12s} {'-----':>6s}")
    for src, count in source_counts.most_common():
        print(f"  {src:<12s} {count:>6d}")


def main():
    parser = argparse.ArgumentParser(description="pre log watcher")
    parser.add_argument("--dump", action="store_true", help="Dump all entries and exit")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--date", type=str, default=None, help="Log date YYYYMMDD (default: today UTC)")
    args = parser.parse_args()

    if args.date:
        log_path = os.path.join(LOG_DIR, f"pre_hook_{args.date}.jsonl")
    else:
        log_path = get_today_log()

    if args.stats:
        stats(log_path)
    elif args.dump:
        dump(log_path)
    else:
        try:
            watch(log_path)
        except KeyboardInterrupt:
            print("\n-- Stopped.")


if __name__ == "__main__":
    main()
