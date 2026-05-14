#!/usr/bin/env python3
"""
pre-repair — 覆盖修复当前目录的 pre wiring (agent_config + .claude hook).

差异 vs pre-init:
  - pre-init: 已存在 → skipped / conflict, 不强改.
  - pre-repair: 已存在 → 覆盖 (backup), 强更 driver 管的字段.

只动两个文件:
  - cwd/pre/agent_config.json (preserve 用户自定义 key, 强更 driver 字段)
  - cwd/.claude/settings.json (重写 hooks.PreToolUse + hooks.Stop, 其他 hook 保留)

绝不动: pre/rules.md, pre/next.md, pre_rule/agents/<dir>/pointer, cwd 之外文件.

用法:
  pre-repair [target_dir] [--mode ...] [--tmux-session ...]
             [--no-claude-settings] [--no-config]

默认 target_dir = $(pwd).
"""
import argparse
import json
import os
import shutil
import sys
import time

C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"


def _backup(path: str) -> str:
    bak = f"{path}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(path, bak)
    return bak


_CLI_TO_DRIVER = {
    "claude": "cli-claude-code-local",
    "codex":  "cli-codex-local",
    "gemini": "cli-gemini-local",
}


def _resolve_driver(opts: dict, existing: dict) -> tuple[str, str]:
    """决定 (cli, driver_type), 落到 agent_config.

    优先级: --driver flag > existing.cli > 交互 prompt (stdin tty) > error.
    返 (cli, driver_type). 任一缺 raise SystemExit.
    """
    cli = opts.get("driver") or existing.get("cli")
    if not cli:
        # 交互 prompt
        if not sys.stdin.isatty():
            raise SystemExit(
                "agent_config.json 缺 cli 字段, 且未传 --driver. "
                "重跑时显式: pre repair --driver claude|codex|gemini"
            )
        print(f"{C_YELLOW}agent_config.json 缺 cli 字段, 选一个 driver:{C_RESET}")
        print("  1. claude  (cli-claude-code-local)")
        print("  2. codex   (cli-codex-local)")
        print("  3. gemini  (cli-gemini-local)")
        while True:
            try:
                ans = input("选 1/2/3 (or 'q' 退): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit("\nuser cancelled")
            if ans in ("q", "quit", "exit"):
                raise SystemExit("user cancelled")
            mapping = {"1": "claude", "claude": "claude",
                       "2": "codex",  "codex":  "codex",
                       "3": "gemini", "gemini": "gemini"}
            if ans in mapping:
                cli = mapping[ans]
                break
            print(f"{C_YELLOW}请选 1/2/3 或输 claude/codex/gemini{C_RESET}")
    if cli not in _CLI_TO_DRIVER:
        raise SystemExit(f"unknown cli '{cli}'; expect one of: {list(_CLI_TO_DRIVER)}")
    return cli, _CLI_TO_DRIVER[cli]


def _repair_agent_config(target_dir: str, opts: dict) -> tuple[str, str]:
    """重写 cwd/pre/agent_config.json. preserve 用户自定义 key.

    返 (cfg_path, backup_path_or_empty)."""
    pre_dir = os.path.join(target_dir, "pre")
    os.makedirs(pre_dir, exist_ok=True)
    cfg_path = os.path.join(pre_dir, "agent_config.json")

    existing: dict = {}
    backup = ""
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path) as f:
                existing = json.load(f)
                if not isinstance(existing, dict):
                    existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        backup = _backup(cfg_path)

    cli, driver_type = _resolve_driver(opts, existing)

    # preserve 用户自定义字段, 强更 driver 管的核心字段
    new = dict(existing)
    new["cli"] = cli
    new["driver_type"] = driver_type
    new["mode"] = opts.get("mode") or existing.get("mode") or "supervised"
    fallback_name = os.path.basename(target_dir.rstrip("/")) or "agent"
    new["tmux_session"] = (
        opts.get("tmux_session") or existing.get("tmux_session") or fallback_name
    )
    project_name = (
        opts.get("project_name") or existing.get("project_name") or fallback_name
    )
    new["project_name"] = project_name
    if opts.get("model"):
        new["model"] = opts["model"]
    if opts.get("role"):
        new["role"] = opts["role"]

    # mcp 块 normalize: server 强制 "pre" (清掉 fn_pre 等 stale 值);
    # caller_agent_id 强制 {node}.{driver}.{project}, 跟 driver discover 用的
    # agent_id 对齐, 不依赖 pre_mcp/tools.py 的 driver_type+project_name fallback.
    node_id = os.environ.get("PRE_NODE_ID", "local")
    mcp_block = new.get("mcp") if isinstance(new.get("mcp"), dict) else {}
    mcp_block["server"] = "pre"
    mcp_block["caller_agent_id"] = f"{node_id}.{driver_type}.{project_name}"
    new["mcp"] = mcp_block

    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return cfg_path, backup


def _repair_pre_rule_mode() -> tuple[str, str, bool]:
    """如果 pre_rule/config.json 的 mode != enforce, 改 enforce (backup 老的).

    返 (cfg_path, backup_or_empty, changed). changed=False 表示已 enforce 或不动.
    用途: 老 install 时 template 是 observe (现在 template 已改 enforce 但 install_pre_rule
    把 config.json 当 global 不覆盖), repair 显式 promote 到 enforce 让 hook 写 marker.
    """
    rule_root = os.environ.get("PRE_RULE_ROOT")
    if not rule_root:
        return ("", "", False)
    cfg_path = os.path.join(rule_root, "config.json")
    if not os.path.isfile(cfg_path):
        return (cfg_path, "", False)  # 没文件不主动建 (install_pre_rule.py 的事)
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return (cfg_path, "", False)
    except (OSError, json.JSONDecodeError):
        return (cfg_path, "", False)
    if cfg.get("mode") == "enforce":
        return (cfg_path, "", False)
    backup = _backup(cfg_path)
    cfg["mode"] = "enforce"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return (cfg_path, backup, True)


def _repair_claude_settings(target_dir: str) -> tuple[str, str]:
    """重写 .claude/settings.json 的 PreToolUse + Stop hooks. 保留其他 hook events."""
    settings_dir = os.path.join(target_dir, ".claude")
    os.makedirs(settings_dir, exist_ok=True)
    settings_path = os.path.join(settings_dir, "settings.json")

    shim = os.path.expanduser("~/.local/bin/pre-tool-use")
    if not os.path.isfile(shim):
        raise SystemExit(
            f"shim {shim} not installed; "
            f"run `bash {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/scripts/install.sh` first"
        )

    existing: dict = {}
    backup = ""
    if os.path.isfile(settings_path):
        try:
            with open(settings_path) as f:
                existing = json.load(f)
                if not isinstance(existing, dict):
                    existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        backup = _backup(settings_path)

    if not isinstance(existing.get("hooks"), dict):
        existing["hooks"] = {}

    existing["hooks"]["PreToolUse"] = [
        {"hooks": [{"type": "command", "command": "pre-tool-use"}]}
    ]
    existing["hooks"]["Stop"] = [
        {"hooks": [{"type": "command", "command": "pre-stop-hook"}]}
    ]

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return settings_path, backup


def main() -> int:
    p = argparse.ArgumentParser(
        prog="pre repair",
        description="Force-rewrite agent_config + .claude/settings hook in cwd.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("target_dir", nargs="?", default=os.getcwd(),
                   help="agent cwd (default: current dir)")
    p.add_argument("--driver", choices=["claude", "codex", "gemini"],
                   default=None,
                   help="cli driver. preserve existing cli if set; else prompt or error")
    p.add_argument("--mode", choices=["supervised", "autonomous", "freerun"],
                   default=None)
    p.add_argument("--tmux-session", default=None)
    p.add_argument("--project-name", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--role", default=None)
    p.add_argument("--no-config", action="store_true",
                   help="skip pre/agent_config.json rewrite")
    p.add_argument("--no-claude-settings", action="store_true",
                   help="skip .claude/settings.json hook rewrite")
    p.add_argument("--no-rule-mode", action="store_true",
                   help="skip pre_rule/config.json mode promotion (default: ensure enforce)")
    args = p.parse_args()

    target = os.path.abspath(args.target_dir)
    print(f"{C_MAGENTA}━━━ pre-repair: {target} ━━━{C_RESET}\n")

    if args.no_config and args.no_claude_settings and args.no_rule_mode:
        print(f"{C_YELLOW}all three --no-* flags set; nothing to do{C_RESET}")
        return 1

    opts: dict = {}
    for k in ("driver", "mode", "tmux_session", "project_name", "model", "role"):
        v = getattr(args, k)
        if v:
            opts[k] = v

    rewritten: list[tuple[str, str]] = []
    rule_mode_msg = ""
    try:
        if not args.no_config:
            rewritten.append(_repair_agent_config(target, opts))
        if not args.no_claude_settings:
            rewritten.append(_repair_claude_settings(target))
        if not args.no_rule_mode:
            cfg_path, bak, changed = _repair_pre_rule_mode()
            if changed:
                rewritten.append((cfg_path, bak))
                rule_mode_msg = "pre_rule mode → enforce (hook 写 marker, fe ui transcript 可见)"
            elif cfg_path:
                rule_mode_msg = f"pre_rule mode 已是 enforce (skip): {cfg_path}"
            else:
                rule_mode_msg = "PRE_RULE_ROOT not set / config.json 不存在 (skip)"
    except SystemExit:
        raise
    except OSError as e:
        print(f"{C_YELLOW}write failed: {e}{C_RESET}")
        return 1

    for path, bak in rewritten:
        print(f"{C_CYAN}[rewrote]{C_RESET}  {path}")
        if bak:
            print(f"{C_DIM}  backup: {bak}{C_RESET}")
    if rule_mode_msg:
        print(f"{C_DIM}[rule]{C_RESET}     {rule_mode_msg}")

    print(f"\n{C_CYAN}━━━ ok ━━━{C_RESET}")
    print(f"{C_DIM}rules.md / next.md / pointer untouched.{C_RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
