#!/usr/bin/env python3
"""install_pre_rule.py — 把 pre/templates/pre_rule/ 复制/同步到 $PRE_RULE_ROOT.

调用方: scripts/install.sh.

分层策略:
- system 类 (system.md / system_analyze.md / .gitignore / README.md / LICENSE):
  每次 install 强制更新. 与模板内容 hash 不同时 backup 旧文件 (.bak.<ts>) +
  覆盖, 打印 diff 摘要. 一致时跳过.
- global 类 (global.md / global_analyze.md / spawn.rc / config.json):
  不存在则从模板创建; 存在则跳过 (尊重用户修改).

返回 exit code 0 (成功 or 全部跳过), 1 (异常).

用法:
    python3 install_pre_rule.py <pre_rule_root> [--templates-dir DIR] [-y]
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

# system 类: 强制更新
_SYSTEM_FILES = {
    "system.md",
    "system_analyze.md",
    ".gitignore",
    "README.md",
    "LICENSE",
}
# global 类: 首次创建, 之后保留
_GLOBAL_FILES = {
    "global.md",
    "global_analyze.md",
    "spawn.rc",
    "config.json",
}


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _diff_summary(old: Path, new: Path, max_lines: int = 8) -> str:
    try:
        old_lines = old.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines = new.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return "(binary or unreadable; cannot diff)"
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"old/{old.name}", tofile=f"new/{new.name}", n=1,
    ))
    if not diff:
        return "(no textual diff)"
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... ({len(diff) - max_lines} more lines)\n"]
    return "".join(diff).rstrip()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def install(rule_root: Path, templates_dir: Path) -> int:
    if not templates_dir.is_dir():
        print(f"FATAL: templates dir not found: {templates_dir}", file=sys.stderr)
        return 1

    rule_root.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    updated: list[tuple[str, str]] = []  # (name, backup_path)
    skipped_unchanged: list[str] = []
    skipped_user_owned: list[str] = []

    for entry in sorted(templates_dir.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        dst = rule_root / name

        if name in _SYSTEM_FILES:
            if not dst.exists():
                _copy(entry, dst)
                created.append(name)
                continue
            if _hash(entry) == _hash(dst):
                skipped_unchanged.append(name)
                continue
            backup = dst.with_suffix(dst.suffix + f".bak.{_ts()}")
            print(f"\n[system] {name} differs from template — backing up + overwriting:")
            print(f"  backup: {backup}")
            print(_diff_summary(dst, entry))
            shutil.move(str(dst), str(backup))
            _copy(entry, dst)
            updated.append((name, str(backup)))
            continue

        if name in _GLOBAL_FILES:
            if dst.exists():
                skipped_user_owned.append(name)
                continue
            _copy(entry, dst)
            created.append(name)
            continue

        # Unknown template file — copy if missing, otherwise skip.
        if dst.exists():
            skipped_user_owned.append(name)
        else:
            _copy(entry, dst)
            created.append(name)

    # spawn.rc 应该可执行 (会被 source, 但有些 user 直接 ./spawn.rc 测试)
    spawn = rule_root / "spawn.rc"
    if spawn.exists():
        try:
            spawn.chmod(spawn.stat().st_mode | 0o111)
        except OSError:
            pass

    print(f"\n✓ pre_rule synced at {rule_root}")
    if created:
        print(f"  created:       {', '.join(created)}")
    if updated:
        print(f"  updated:       {', '.join(n for n, _ in updated)} (system layer)")
    if skipped_user_owned:
        print(f"  kept (user):   {', '.join(skipped_user_owned)}")
    if skipped_unchanged:
        print(f"  kept (no-op):  {', '.join(skipped_unchanged)}")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("rule_root", help="absolute path to pre_rule directory")
    p.add_argument("--templates-dir", default=None,
                   help="override templates source dir (default: <pre>/templates/pre_rule)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="non-interactive (reserved; currently install runs idempotent)")
    args = p.parse_args(argv)

    rule_root = Path(args.rule_root).expanduser().resolve()
    if args.templates_dir:
        templates_dir = Path(args.templates_dir).expanduser().resolve()
    else:
        # __file__ = <pre>/scripts/install_pre_rule.py
        here = Path(__file__).resolve().parent
        templates_dir = here.parent / "templates" / "pre_rule"

    return install(rule_root, templates_dir)


if __name__ == "__main__":
    sys.exit(main())
