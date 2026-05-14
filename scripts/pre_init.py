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
import json
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


def _existing_cli(target_dir: str) -> str:
    """读 cwd/pre/agent_config.json 的 cli 字段. 缺/坏返 ""."""
    cfg_path = os.path.join(target_dir, "pre", "agent_config.json")
    if not os.path.isfile(cfg_path):
        return ""
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(cfg, dict):
        return ""
    cli = cfg.get("cli")
    return cli if isinstance(cli, str) and cli in _DRIVERS else ""


def _prompt_driver() -> str:
    """tty 交互选 driver. 非 tty 调用方应先 SystemExit."""
    print(f"{C_YELLOW}新项目无 agent_config.json, 选一个 driver:{C_RESET}")
    print("  1. claude  (cli-claude-code-local)")
    print("  2. codex   (cli-codex-local)")
    print("  3. gemini  (cli-gemini-local)")
    mapping = {"1": "claude", "claude": "claude",
               "2": "codex",  "codex":  "codex",
               "3": "gemini", "gemini": "gemini"}
    while True:
        try:
            ans = input("选 1/2/3 (or 'q' 退): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nuser cancelled")
        if ans in ("q", "quit", "exit"):
            raise SystemExit("user cancelled")
        if ans in mapping:
            return mapping[ans]
        print(f"{C_YELLOW}请选 1/2/3 或输 claude/codex/gemini{C_RESET}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Initialize an agent (claude/codex/gemini) in target_dir (idempotent).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("target_dir", nargs="?", default=os.getcwd(),
                   help="absolute path to agent cwd (default: current dir)")
    p.add_argument("--driver", default=None,
                   choices=sorted(_DRIVERS.keys()),
                   help="cli driver: claude | codex | gemini. 新项目必传 "
                        "(或 tty 下交互选). 已有 agent_config.cli 时可省 (honor existing).")
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

    # driver 决定: explicit flag > existing agent_config.cli > prompt (tty) > error.
    driver_key = args.driver
    if driver_key is None:
        driver_key = _existing_cli(target_dir)
        if driver_key:
            print(f"{C_DIM}driver: {driver_key} (from existing agent_config.cli){C_RESET}")
        else:
            if not sys.stdin.isatty():
                print(
                    f"{C_YELLOW}target_dir 没有 agent_config.json 且未传 --driver. "
                    f"非交互环境必须显式: pre init --driver claude|codex|gemini{C_RESET}",
                    file=sys.stderr,
                )
                return 2
            driver_key = _prompt_driver()
    elif driver_key:
        # 显式 --driver, 但 existing cli 跟它不一致 → 早报错 (driver.init_agent 也会
        # report conflict, 但这里 fail-fast 给用户清晰提示).
        ex_cli = _existing_cli(target_dir)
        if ex_cli and ex_cli != driver_key:
            print(
                f"{C_YELLOW}cli mismatch: agent_config.cli={ex_cli} 但 --driver={driver_key}. "
                f"要换 driver 用 `pre repair --driver {driver_key}` (会覆盖){C_RESET}",
                file=sys.stderr,
            )
            return 2

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

    driver_cls = _DRIVERS[driver_key]

    print(f"{C_MAGENTA}━━━ pre-init: {os.path.basename(target_dir) or '/'} ━━━{C_RESET}")
    print(f"{C_DIM}driver: {driver_key} ({driver_cls.__name__}){C_RESET}")
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
