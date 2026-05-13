#!/usr/bin/env python3
"""
pre-migrate — 一次性迁移老 pre_rule/agents/<dir>/ 到新格式.

老格式: 目录名编码 cwd (Users-X-Y → /X/Y), 没 agent_pointer.json.
新格式: 必须有 agent_pointer.json {cwd, agent_id, cli, project_name, created_at}.

本工具做的:
  1. 扫 pre_rule/agents/<dir>/ 没 pointer 的目录
  2. 从 dir-name 反推 cwd 作为猜测 (老编码方式)
  3. 显示当前状态 (cwd 存在? pre/ 全? cli 是啥?) 让用户确认 / 改 cwd / 跳过 / 删
  4. 写 agent_pointer.json
  5. 提示用户去 cwd 跑 pre-init (若 pre/agent_config.json 不全)

本工具不做的 (按用户决策):
  - 不创建 cwd/pre/agent_config.json (那是 pre-init 干的)
  - 不 backfill agent_config.json 到 pre_rule/agents/<dir>/
  - 不在无用户确认时写 pointer (除非 --yes)

用法:
  pre-migrate [--dry-run] [--yes] [--only <name1> <name2> ...]
"""
import argparse
import json
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRE_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PRE_ROOT, "src"))

from config import RULE_ROOT  # noqa: E402  pre_rule 同 driver 复用


C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"

_DRIVER_TYPE_BY_CLI = {
    "claude": "cli-claude-code-local",
    "codex": "cli-codex-local",
}


def _infer_cwd_from_name(name: str) -> str:
    """老约定: dir_name = cwd.strip('/').replace('/', '-'). 反推."""
    return "/" + name.replace("-", "/")


def _read_project_cfg(cwd: str) -> dict:
    cfg_path = os.path.join(cwd, "pre", "agent_config.json")
    if not os.path.isfile(cfg_path):
        return {}
    try:
        with open(cfg_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _prompt(msg: str) -> str:
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _migrate_one(name: str, agent_pre_dir: str, *,
                 node_id: str, dry_run: bool, auto_yes: bool) -> str:
    """单个 pre_rule/agents/<name>/ → migrated | skipped | failed | already-ok"""
    pointer_path = os.path.join(agent_pre_dir, "agent_pointer.json")
    if os.path.isfile(pointer_path):
        return "already-ok"

    inferred_cwd = _infer_cwd_from_name(name)
    cwd_exists = os.path.isdir(inferred_cwd)
    has_pre_dir = os.path.isdir(os.path.join(inferred_cwd, "pre")) if cwd_exists else False
    cfg = _read_project_cfg(inferred_cwd) if cwd_exists else {}

    print(f"\n{C_MAGENTA}━━━ {name} ━━━{C_RESET}")
    print(f"{C_DIM}pre_rule_dir: {agent_pre_dir}{C_RESET}")
    print(f"{C_BLUE}inferred cwd:{C_RESET} {inferred_cwd}")
    cwd_color = C_CYAN if cwd_exists else C_YELLOW
    pre_color = C_CYAN if has_pre_dir else C_YELLOW
    print(f"  cwd exists: {cwd_color}{cwd_exists}{C_RESET}")
    print(f"  pre/ dir:   {pre_color}{has_pre_dir}{C_RESET}")
    if cfg:
        print(f"  cli: {cfg.get('cli', 'claude')}, mode: {cfg.get('mode')}, "
              f"tmux: {cfg.get('tmux_session')}")
    elif cwd_exists:
        print(f"  {C_YELLOW}no agent_config.json — run pre-init in cwd after migrate{C_RESET}")

    if auto_yes:
        answer = "y"
    else:
        answer = _prompt(
            f"\n{C_BLUE}accept inferred cwd? "
            f"[{C_CYAN}y{C_BLUE}]es / [n]ew cwd / [s]kip / [d]elete this pre_rule dir: {C_RESET}"
        ).lower() or "y"

    cwd_to_write = inferred_cwd
    if answer == "n":
        new_cwd = _prompt(f"  enter new cwd (absolute path): ")
        if not new_cwd or not os.path.isabs(new_cwd):
            print(f"  {C_YELLOW}not absolute, skipping{C_RESET}")
            return "skipped"
        cwd_to_write = new_cwd
    elif answer == "s":
        print(f"  {C_DIM}skipped{C_RESET}")
        return "skipped"
    elif answer == "d":
        if dry_run:
            print(f"  {C_DIM}(dry-run) would remove {agent_pre_dir}{C_RESET}")
            return "migrated"
        confirm = _prompt(f"  type 'delete' to confirm removal of {agent_pre_dir}: ")
        if confirm != "delete":
            print(f"  {C_YELLOW}not confirmed, skipping{C_RESET}")
            return "skipped"
        try:
            shutil.rmtree(agent_pre_dir)
            print(f"  {C_CYAN}removed {agent_pre_dir}{C_RESET}")
            return "migrated"
        except OSError as e:
            print(f"  {C_YELLOW}rm failed: {e}{C_RESET}")
            return "failed"
    elif answer != "y" and answer != "":
        print(f"  {C_YELLOW}unknown answer '{answer}', skipping{C_RESET}")
        return "skipped"

    cli = cfg.get("cli") or "claude"
    project_name = cfg.get("project_name") or os.path.basename(cwd_to_write.rstrip("/")) or name
    driver_type = _DRIVER_TYPE_BY_CLI.get(cli, f"cli-{cli}-local")

    pointer = {
        "cwd": cwd_to_write,
        "agent_id": f"{node_id}.{driver_type}.{project_name}",
        "cli": cli,
        "project_name": project_name,
        "created_at": time.time(),
        "_migrated_from": name,
    }

    if dry_run:
        print(f"  {C_DIM}(dry-run) would write {pointer_path}:{C_RESET}")
        print(f"  {C_DIM}{json.dumps(pointer, indent=2)}{C_RESET}")
        return "migrated"

    try:
        with open(pointer_path, "w", encoding="utf-8") as f:
            json.dump(pointer, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"  {C_CYAN}wrote {pointer_path}{C_RESET}")
    except OSError as e:
        print(f"  {C_YELLOW}write failed: {e}{C_RESET}")
        return "failed"

    if cwd_exists and not has_pre_dir:
        print(f"  {C_YELLOW}note: {cwd_to_write}/pre/ missing — "
              f"run `pre-init` in {cwd_to_write} to finish{C_RESET}")
    elif cwd_exists and not cfg:
        print(f"  {C_YELLOW}note: {cwd_to_write}/pre/agent_config.json missing — "
              f"run `pre-init` in {cwd_to_write}{C_RESET}")
    elif not cwd_exists:
        print(f"  {C_YELLOW}note: cwd {cwd_to_write} does not exist on disk — "
              f"create it then `pre-init`{C_RESET}")

    return "migrated"


def main() -> int:
    p = argparse.ArgumentParser(
        description="One-shot migration of legacy pre_rule/agents/<dir>/ to pointer format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--rule-root", default=None,
                   help=f"override pre_rule root (default: {RULE_ROOT})")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true",
                   help="auto-accept inferred cwd for all (use only in CI)")
    p.add_argument("--node-id", default=os.environ.get("PRE_NODE_ID", "local"))
    p.add_argument("--only", nargs="*", default=None,
                   help="only migrate these dir names (default: all without pointer)")
    args = p.parse_args()

    rule_root = args.rule_root or os.environ.get("PRE_RULE_ROOT") or RULE_ROOT
    agents_dir = os.path.join(rule_root, "agents")

    if not os.path.isdir(agents_dir):
        print(f"{C_YELLOW}agents dir not found: {agents_dir}{C_RESET}")
        return 1

    print(f"{C_MAGENTA}━━━ pre-migrate ━━━{C_RESET}")
    print(f"{C_DIM}rule_root: {rule_root}{C_RESET}")
    print(f"{C_DIM}dry_run:   {args.dry_run}{C_RESET}")
    print(f"{C_DIM}auto_yes:  {args.yes}{C_RESET}")

    targets = []
    for name in sorted(os.listdir(agents_dir)):
        agent_pre_dir = os.path.join(agents_dir, name)
        if not os.path.isdir(agent_pre_dir):
            continue
        if args.only and name not in args.only:
            continue
        targets.append((name, agent_pre_dir))

    if not targets:
        print(f"\n{C_DIM}no target dirs.{C_RESET}")
        return 0

    counts = {"migrated": 0, "skipped": 0, "failed": 0, "already-ok": 0}
    for name, agent_pre_dir in targets:
        try:
            status = _migrate_one(name, agent_pre_dir,
                                  node_id=args.node_id,
                                  dry_run=args.dry_run,
                                  auto_yes=args.yes)
        except Exception as e:
            print(f"  {C_YELLOW}unexpected: {e}{C_RESET}")
            status = "failed"
        counts[status] = counts.get(status, 0) + 1

    print(f"\n{C_MAGENTA}━━━ summary ━━━{C_RESET}")
    print(f"  {C_CYAN}migrated{C_RESET}:   {counts['migrated']}")
    print(f"  {C_DIM}skipped{C_RESET}:    {counts['skipped']}")
    print(f"  {C_DIM}already-ok{C_RESET}: {counts['already-ok']}")
    print(f"  {C_YELLOW}failed{C_RESET}:     {counts['failed']}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
