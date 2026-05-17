#!/usr/bin/env python3
"""
gover_view.py — 查看 codex / gemini driver 的 governor 审批结果 (send-keys auto-approve/reject)

数据源:
  ~/cursor/pre_log/codex_driver/auto_decision_YYYYMMDD.jsonl
  ~/cursor/pre_log/gemini_driver/auto_decision_YYYYMMDD.jsonl

每条 entry 字段 (driver 写入, 见 src/drivers/cli_{codex,gemini}_local/driver.py):
  ts, agent_id, cwd, tmux_session, tool_name, tool_input_preview,
  decision (allow|deny|ask), reason, source (local|cache|governor|...),
  action (approve_key_sent|reject_key_sent|reported_to_user), ok

用法:
  uv run python scripts/gover_view.py                    # 今日 codex+gemini 全部
  uv run python scripts/gover_view.py --follow           # tail -F 实时
  uv run python scripts/gover_view.py --stats            # 决策汇总
  uv run python scripts/gover_view.py --driver codex     # 仅 codex driver
  uv run python scripts/gover_view.py --decision deny    # 仅 deny
  uv run python scripts/gover_view.py --source governor  # 仅 LLM 决策 (跳过 local/cache)
  uv run python scripts/gover_view.py --agent test_gemini # agent_id 子串匹配
  uv run python scripts/gover_view.py --date 20260513    # 指定日期
  uv run python scripts/gover_view.py --json             # 原始 JSONL 输出

HC-PRE-1 stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from common.paths import PRE_LOG_ROOT  # noqa: E402

DRIVERS = ("codex", "gemini")
DECISIONS = ("allow", "deny", "ask")
SOURCES = (
    "local",
    "cache",
    "governor",
    "governor_no_cache",
    "observe",
    "fallback",
    "driver_passthrough",
    "driver_fail_closed",
)
ACTIONS = ("approve_key_sent", "reject_key_sent", "reported_to_user")

# ANSI 颜色: tty 才上色, 管道/重定向自动关
def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") not in ("dumb", "")

_COLOR = _supports_color()
_C = {
    "reset": "\033[0m" if _COLOR else "",
    "dim": "\033[2m" if _COLOR else "",
    "bold": "\033[1m" if _COLOR else "",
    "green": "\033[32m" if _COLOR else "",
    "red": "\033[31m" if _COLOR else "",
    "yellow": "\033[33m" if _COLOR else "",
    "cyan": "\033[36m" if _COLOR else "",
    "magenta": "\033[35m" if _COLOR else "",
}


def _audit_path(driver: str, date_str: str) -> Path:
    return Path(PRE_LOG_ROOT) / f"{driver}_driver" / f"auto_decision_{date_str}.jsonl"


def _iter_file(path: Path) -> Iterator[dict]:
    """逐行读 JSONL, 损坏的行跳过."""
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _match(entry: dict, args: argparse.Namespace) -> bool:
    if args.decision and entry.get("decision") != args.decision:
        return False
    if args.source and entry.get("source") != args.source:
        return False
    if args.action and entry.get("action") != args.action:
        return False
    if args.agent:
        agent_id = str(entry.get("agent_id") or "")
        if args.agent not in agent_id:
            return False
    if args.tool and str(entry.get("tool_name") or "").lower() != args.tool.lower():
        return False
    return True


def _color_for(field: str, value: str) -> str:
    if field == "decision":
        return {
            "allow": _C["green"],
            "deny": _C["red"],
            "ask": _C["yellow"],
        }.get(value, "")
    if field == "source":
        if value == "governor":
            return _C["magenta"]
        if value in ("local", "cache"):
            return _C["dim"]
        return _C["yellow"]
    if field == "action":
        if value == "approve_key_sent":
            return _C["green"]
        if value == "reject_key_sent":
            return _C["red"]
        if value == "reported_to_user":
            return _C["yellow"]
    return ""


def _fmt_ts(ts: str) -> str:
    # ts 是 ISO UTC; 截短显示 HH:MM:SS, 多日时附 MM-DD
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:19]
    local = dt.astimezone()
    return local.strftime("%m-%d %H:%M:%S")


def _short(s: str, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_line(driver: str, e: dict, verbose: bool) -> str:
    ts = _fmt_ts(str(e.get("ts") or ""))
    decision = str(e.get("decision") or "?")
    source = str(e.get("source") or "?")
    action = str(e.get("action") or "?")
    tool = str(e.get("tool_name") or "?")
    agent = str(e.get("agent_id") or "?").split(".")[-1]  # 末尾 project_name
    preview = _short(str(e.get("tool_input_preview") or ""), 60)
    reason = _short(str(e.get("reason") or ""), 80)

    dc = _color_for("decision", decision)
    sc = _color_for("source", source)
    ac = _color_for("action", action)
    R = _C["reset"]

    parts = [
        f"{_C['dim']}{ts}{R}",
        f"{_C['cyan']}{driver:>6}{R}",
        f"{agent:<18}",
        f"{tool:<8}",
        f"{dc}{decision:<5}{R}",
        f"{sc}{source:<17}{R}",
        f"{ac}{action:<19}{R}",
        preview,
    ]
    line = " ".join(parts)
    if verbose and reason:
        line += f"\n  {_C['dim']}↳ {reason}{R}"
    return line


def _header() -> str:
    cols = ["ts          ", "driver", "agent             ", "tool    ",
            "deci ", "source           ", "action             ", "input"]
    return _C["bold"] + " ".join(cols) + _C["reset"]


def _collect(args: argparse.Namespace) -> list[tuple[str, dict]]:
    """读两份 driver 当日文件, 合并按 ts 排序. 返 (driver, entry) tuple."""
    out: list[tuple[str, dict]] = []
    drivers = (args.driver,) if args.driver else DRIVERS
    for d in drivers:
        for e in _iter_file(_audit_path(d, args.date)):
            if _match(e, args):
                out.append((d, e))
    out.sort(key=lambda t: t[1].get("ts", ""))
    if args.limit and args.limit > 0:
        out = out[-args.limit:]
    return out


def _print_stats(rows: list[tuple[str, dict]]) -> None:
    if not rows:
        print(f"{_C['dim']}(no entries match){_C['reset']}")
        return

    by_decision: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_driver: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    by_tool: dict[str, int] = {}

    for d, e in rows:
        by_driver[d] = by_driver.get(d, 0) + 1
        by_decision[str(e.get("decision") or "?")] = by_decision.get(str(e.get("decision") or "?"), 0) + 1
        by_source[str(e.get("source") or "?")] = by_source.get(str(e.get("source") or "?"), 0) + 1
        by_action[str(e.get("action") or "?")] = by_action.get(str(e.get("action") or "?"), 0) + 1
        agent = str(e.get("agent_id") or "?").split(".")[-1]
        by_agent[agent] = by_agent.get(agent, 0) + 1
        by_tool[str(e.get("tool_name") or "?")] = by_tool.get(str(e.get("tool_name") or "?"), 0) + 1

    total = len(rows)
    B, R = _C["bold"], _C["reset"]
    print(f"{B}total{R}: {total}")

    def _dump(title: str, d: dict[str, int], color_field: Optional[str] = None) -> None:
        print(f"\n{B}{title}{R}")
        for k in sorted(d, key=lambda x: -d[x]):
            c = _color_for(color_field, k) if color_field else ""
            bar = "█" * min(40, d[k] * 40 // total) if total else ""
            print(f"  {c}{k:<20}{R} {d[k]:>5}  {_C['dim']}{bar}{R}")

    _dump("by driver", by_driver)
    _dump("by decision", by_decision, "decision")
    _dump("by source", by_source, "source")
    _dump("by action", by_action, "action")
    _dump("by tool", by_tool)
    if len(by_agent) > 1:
        _dump("by agent", by_agent)


def _follow(args: argparse.Namespace) -> None:
    """tail -F 模式: 每 0.5s 轮询两份文件, 输出新行. Ctrl-C 退出."""
    drivers = (args.driver,) if args.driver else DRIVERS
    offsets: dict[str, int] = {}

    # 初始化: 跳到文件末尾, 只看新增
    for d in drivers:
        p = _audit_path(d, args.date)
        if p.is_file():
            offsets[d] = p.stat().st_size
        else:
            offsets[d] = 0

    if not args.no_header:
        print(_header(), flush=True)
        print(f"{_C['dim']}(follow mode — Ctrl-C to exit; date={args.date}){_C['reset']}", flush=True)

    try:
        while True:
            for d in drivers:
                p = _audit_path(d, args.date)
                if not p.is_file():
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size < offsets[d]:
                    # 文件被截断/日期翻页, 重置
                    offsets[d] = 0
                if size == offsets[d]:
                    continue
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(offsets[d])
                    chunk = f.read()
                    offsets[d] = f.tell()
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not _match(e, args):
                        continue
                    if args.json:
                        print(json.dumps(e, ensure_ascii=False), flush=True)
                    else:
                        print(_fmt_line(d, e, args.verbose), flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{_C['dim']}(stopped){_C['reset']}", file=sys.stderr)


def _print_dump(rows: list[tuple[str, dict]], args: argparse.Namespace) -> None:
    if not rows:
        # 帮 user 排查空结果
        paths = [str(_audit_path(d, args.date)) for d in (DRIVERS if not args.driver else (args.driver,))]
        existing = [p for p in paths if Path(p).is_file()]
        print(f"{_C['dim']}(no entries match){_C['reset']}")
        if not existing:
            print(f"{_C['dim']}files not found:{_C['reset']}")
            for p in paths:
                print(f"  - {p}")
        return

    if args.json:
        for _, e in rows:
            print(json.dumps(e, ensure_ascii=False))
        return

    if not args.no_header:
        print(_header())
    for d, e in rows:
        print(_fmt_line(d, e, args.verbose))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def main() -> int:
    p = argparse.ArgumentParser(
        prog="gover_view",
        description="查看 codex/gemini driver 的 governor 审批结果 (auto_decision_*.jsonl)",
    )
    p.add_argument("--driver", choices=DRIVERS, default=None,
                   help="只看一个 driver (默认两个都看)")
    p.add_argument("--decision", choices=DECISIONS, default=None,
                   help="过滤决策 (allow/deny/ask)")
    p.add_argument("--source", choices=SOURCES, default=None,
                   help="过滤决策来源 (local/cache/governor/...)")
    p.add_argument("--action", choices=ACTIONS, default=None,
                   help="过滤动作 (approve_key_sent/reject_key_sent/reported_to_user)")
    p.add_argument("--agent", default=None,
                   help="agent_id 子串过滤 (e.g. test_gemini)")
    p.add_argument("--tool", default=None,
                   help="按 tool_name 过滤 (e.g. Bash, Edit)")
    p.add_argument("--date", default=_today(),
                   help="日期 YYYYMMDD (默认今天 UTC)")
    p.add_argument("-n", "--limit", type=int, default=0,
                   help="只显示最后 N 条 (0=全部)")
    p.add_argument("-f", "--follow", action="store_true",
                   help="tail -F 实时模式")
    p.add_argument("--stats", action="store_true",
                   help="按 decision/source/action/agent/tool 统计")
    p.add_argument("--json", action="store_true",
                   help="输出原始 JSONL (便于 jq 后处理)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="附带 reason 行")
    p.add_argument("--no-header", action="store_true",
                   help="不输出表头")
    p.add_argument("--list-paths", action="store_true",
                   help="只打印 audit 文件路径并退出")

    args = p.parse_args()

    if args.list_paths:
        drivers = (args.driver,) if args.driver else DRIVERS
        for d in drivers:
            path = _audit_path(d, args.date)
            mark = "✓" if path.is_file() else "✗"
            print(f"{mark} {path}")
        return 0

    if args.follow:
        if args.stats:
            print("error: --follow 跟 --stats 互斥", file=sys.stderr)
            return 2
        _follow(args)
        return 0

    rows = _collect(args)

    if args.stats:
        _print_stats(rows)
    else:
        _print_dump(rows, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
