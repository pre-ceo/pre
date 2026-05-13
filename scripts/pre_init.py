#!/usr/bin/env python3
"""
pre-init — 在指定目录初始化一个 agent (幂等). 支持 claude / codex / gemini.

用法:
  pre-init [target_dir] [--driver claude|codex|gemini] [...]

默认 target_dir = $(pwd), 默认 --driver claude.

输出 InitResult: 创建 / 跳过 / 冲突 / 下一步.
exit 0 = ok, 1 = 有 conflict / failure / tmux 未起.

行为:
- 配置真源 = target_dir/pre/agent_config.json (cli 字段 = driver 选择).
- pre_rule/agents/<dir>/agent_pointer.json 是 driver 索引指针 (含 cwd + cli).
- claude: 额外写 .claude/settings.json hook (PreToolUse + Stop).
- codex/gemini: 无 hook 接口, approval 走 driver 内嵌 evaluator.
- 已有 cli 跟 --driver 不一致 → 报 conflicts (不强改).
"""
import argparse
import asyncio
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRE_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PRE_ROOT, "src"))

from drivers.cli_claude_code_local.driver import CliClaudeCodeLocalDriver  # noqa: E402
from drivers.cli_codex_local.driver import CliCodexLocalDriver  # noqa: E402


# driver registry: --driver value → driver class
_DRIVERS: dict = {
    "claude": CliClaudeCodeLocalDriver,
    "codex": CliCodexLocalDriver,
}
# gemini driver 在 src/drivers/cli_gemini_local/ 存在时自动加载
try:
    from drivers.cli_gemini_local.driver import CliGeminiLocalDriver  # noqa: E402
    _DRIVERS["gemini"] = CliGeminiLocalDriver
except ImportError:
    pass


C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"


def _detect_tmux_session() -> str:
    if not os.environ.get("TMUX"):
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return ""


async def _run(driver_cls, target_dir: str, opts: dict):
    driver = driver_cls()
    node_id = os.environ.get("PRE_NODE_ID", "local")
    await driver.init({"node_id": node_id})
    return await driver.init_agent(target_dir, opts)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Initialize an agent (claude/codex/gemini) in target_dir (idempotent).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("target_dir", nargs="?", default=os.getcwd(),
                   help="absolute path to agent cwd (default: current dir)")
    p.add_argument("--driver", default="claude",
                   choices=sorted(_DRIVERS.keys()),
                   help="cli driver: claude (default) | codex | gemini")
    p.add_argument("--mode", default="supervised",
                   choices=["supervised", "autonomous", "freerun"])
    p.add_argument("--tmux-session", default=None,
                   help="tmux session (default: $TMUX session or basename)")
    p.add_argument("--project-name", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--role", default=None)
    p.add_argument("--no-claude-settings", action="store_true",
                   help="skip .claude/settings.json hook install (claude only)")
    p.add_argument("--no-templates", action="store_true",
                   help="skip rules.md / next.md templates")
    args = p.parse_args()

    target_dir = os.path.abspath(args.target_dir)

    if args.tmux_session is None:
        ts = _detect_tmux_session() or (os.path.basename(target_dir.rstrip("/")) or "agent")
    else:
        ts = args.tmux_session

    opts = {
        "mode": args.mode,
        "tmux_session": ts,
        "write_claude_settings": not args.no_claude_settings,
        "write_templates": not args.no_templates,
    }
    if args.project_name:
        opts["project_name"] = args.project_name
    if args.model:
        opts["model"] = args.model
    if args.role:
        opts["role"] = args.role

    driver_cls = _DRIVERS[args.driver]

    print(f"{C_MAGENTA}━━━ pre-init: {os.path.basename(target_dir) or '/'} ━━━{C_RESET}")
    print(f"{C_DIM}driver: {args.driver} ({driver_cls.__name__}){C_RESET}")
    print(f"{C_DIM}target: {target_dir}{C_RESET}")
    print(f"{C_DIM}tmux:   {ts}{C_RESET}")
    print(f"{C_DIM}mode:   {args.mode}{C_RESET}\n")

    result = asyncio.run(_run(driver_cls, target_dir, opts))

    if result.agent_id:
        print(f"{C_BLUE}[agent_id]{C_RESET} {result.agent_id}\n")

    for path in result.created:
        print(f"{C_CYAN}[created]{C_RESET}  {path}")
    for path in result.skipped:
        print(f"{C_DIM}[skipped]{C_RESET}  {path}")
    for c in result.conflicts:
        print(f"{C_YELLOW}[conflict]{C_RESET} {c}")
    for f in result.failures:
        print(f"{C_YELLOW}[failure]{C_RESET} {f}")

    if result.next_steps:
        print(f"\n{C_MAGENTA}── next steps ──{C_RESET}")
        for n in result.next_steps:
            print(f"{C_BLUE}→{C_RESET} {n}")

    print()
    if result.ok:
        print(f"{C_CYAN}━━━ ok ━━━{C_RESET}")
        return 0
    print(f"{C_YELLOW}━━━ incomplete ━━━{C_RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
