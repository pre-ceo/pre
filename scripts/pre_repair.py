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

    # preserve 用户自定义字段, 强更 driver 管的核心 4 字段
    new = dict(existing)
    new["cli"] = "claude"
    new["mode"] = opts.get("mode") or existing.get("mode") or "supervised"
    fallback_name = os.path.basename(target_dir.rstrip("/")) or "agent"
    new["tmux_session"] = (
        opts.get("tmux_session") or existing.get("tmux_session") or fallback_name
    )
    new["project_name"] = (
        opts.get("project_name") or existing.get("project_name") or fallback_name
    )
    if opts.get("model"):
        new["model"] = opts["model"]
    if opts.get("role"):
        new["role"] = opts["role"]

    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(new, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return cfg_path, backup


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
    args = p.parse_args()

    target = os.path.abspath(args.target_dir)
    print(f"{C_MAGENTA}━━━ pre-repair: {target} ━━━{C_RESET}\n")

    if args.no_config and args.no_claude_settings:
        print(f"{C_YELLOW}both --no-config and --no-claude-settings set; nothing to do{C_RESET}")
        return 1

    opts: dict = {}
    for k in ("mode", "tmux_session", "project_name", "model", "role"):
        v = getattr(args, k)
        if v:
            opts[k] = v

    rewritten: list[tuple[str, str]] = []
    try:
        if not args.no_config:
            rewritten.append(_repair_agent_config(target, opts))
        if not args.no_claude_settings:
            rewritten.append(_repair_claude_settings(target))
    except SystemExit:
        raise
    except OSError as e:
        print(f"{C_YELLOW}write failed: {e}{C_RESET}")
        return 1

    for path, bak in rewritten:
        print(f"{C_CYAN}[rewrote]{C_RESET}  {path}")
        if bak:
            print(f"{C_DIM}  backup: {bak}{C_RESET}")

    print(f"\n{C_CYAN}━━━ ok ━━━{C_RESET}")
    print(f"{C_DIM}rules.md / next.md / pointer untouched.{C_RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
