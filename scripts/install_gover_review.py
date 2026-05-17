#!/usr/bin/env python3
"""gover_review 安装 entry — install.sh / pre_update.py 末尾调.

做 4 件事 (幂等):
  1. pre_init.py <workdir> --driver claude --no-templates → 写 pointer + .claude/settings.json
  2. install_workdir(workdir, force=True) → 覆盖 agent_config / next.md / rules.md
  3. install_schedule(trigger.sh, schedules.json) → cron entry merge
  4. fire_initial_trigger() → 异步 fire-and-forget 跑一次 trigger.sh (cron interval 首次
     也会立即跑, 这是双保险)

任一步 subprocess 失败仅 warn, 不阻塞 install/update 主流程.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PRE_ROOT = _HERE.parent
if str(_PRE_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PRE_ROOT / "src"))

from gover_review.cron_install import install_schedule  # noqa: E402
from gover_review.install_agent import DEFAULT_WORKDIR, install_workdir  # noqa: E402

C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"

TRIGGER_REL = "scripts/gover_review/cron_trigger.sh"


def resolve_pre_rule_root() -> Path:
    """优先 ~/.pre/env::PRE_RULE_ROOT, fallback sibling."""
    env_file = Path.home() / ".pre" / "env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line.startswith("PRE_RULE_ROOT="):
                continue
            v = line.split("=", 1)[1]
            v = v.split("#", 1)[0].strip().strip('"').strip("'")
            if v:
                return Path(v)
    return _PRE_ROOT.parent / "pre_rule"


def run_pre_init(workdir: Path, *, pre_root: Path = _PRE_ROOT) -> int:
    """调 pre_init.py 注册 agent. --no-templates 让 pre_init 不覆盖我们的模板."""
    pre_init = pre_root / "scripts" / "pre_init.py"
    if not pre_init.exists():
        print(
            f"{C_YELLOW}[warn] pre_init.py not found at {pre_init}{C_RESET}"
        )
        return 1
    cmd = [
        "python3",
        str(pre_init),
        str(workdir),
        "--driver",
        "claude",
        "--mode",
        "supervised",
        "--no-templates",
    ]
    try:
        return subprocess.call(cmd)
    except OSError as e:
        print(f"{C_YELLOW}[warn] pre_init invoke failed: {e}{C_RESET}")
        return 1


def fire_initial_trigger(trigger_script: Path) -> int:
    """异步 fire-and-forget 跑 cron_trigger.sh. 不阻塞 install."""
    if not trigger_script.exists():
        print(
            f"{C_YELLOW}[warn] trigger script missing: {trigger_script}{C_RESET}"
        )
        return 1
    try:
        subprocess.Popen(
            ["bash", str(trigger_script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return 0
    except OSError as e:
        print(f"{C_YELLOW}[warn] initial trigger failed: {e}{C_RESET}")
        return 1


def main(
    *,
    workdir: Path = DEFAULT_WORKDIR,
    pre_root: Path = _PRE_ROOT,
    rule_root: Path | None = None,
    skip_pre_init: bool = False,
    skip_initial_trigger: bool = False,
) -> int:
    if rule_root is None:
        rule_root = resolve_pre_rule_root()
    trigger_script = pre_root / TRIGGER_REL
    schedules_file = rule_root / "cron" / "schedules.json"

    print(f"\n{C_MAGENTA}━━━ gover_review install ━━━{C_RESET}")
    print(f"{C_DIM}  workdir        = {workdir}{C_RESET}")
    print(f"{C_DIM}  trigger.sh     = {trigger_script}{C_RESET}")
    print(f"{C_DIM}  schedules.json = {schedules_file}{C_RESET}")

    if not skip_pre_init:
        rc = run_pre_init(workdir, pre_root=pre_root)
        if rc != 0:
            print(
                f"{C_YELLOW}[warn] pre_init rc={rc} — agent 未正确注册, spawn 时可能失败{C_RESET}"
            )

    r = install_workdir(workdir, force=True)
    if r["errors"]:
        print(f"{C_YELLOW}[error] install_workdir: {r['errors']}{C_RESET}")
        return 1
    print(
        f"{C_CYAN}[ok]{C_RESET} templates installed "
        f"({len(r['created'])} files at {r['pre_dir']})"
    )

    r2 = install_schedule(trigger_script, schedules_file)
    print(
        f"{C_CYAN}[ok]{C_RESET} cron schedule merged → {r2['schedules_file']}"
    )
    print(
        f"{C_DIM}     id={r2['entry']['id']} "
        f"every={r2['entry']['every_seconds']}s target=local{C_RESET}"
    )

    if not skip_initial_trigger:
        if fire_initial_trigger(trigger_script) == 0:
            print(
                f"{C_CYAN}[ok]{C_RESET} initial trigger fired (async, fire-and-forget)"
            )
        else:
            print(
                f"{C_DIM}[skip] initial trigger; cron 30s 内会接力{C_RESET}"
            )

    print(f"{C_MAGENTA}━━━ gover_review ok ━━━{C_RESET}\n")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="install_gover_review",
        description="Install gover_review agent (workdir + pre init + cron + trigger).",
    )
    p.add_argument(
        "--skip-pre-init", action="store_true", help="skip pre_init.py call"
    )
    p.add_argument(
        "--skip-trigger",
        action="store_true",
        help="skip initial async trigger fire",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(
        main(
            skip_pre_init=args.skip_pre_init,
            skip_initial_trigger=args.skip_trigger,
        )
    )
