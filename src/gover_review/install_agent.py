"""gover_review workdir 模板安装.

职责窄: 把 scripts/gover_review/templates/ 下的 agent_config.json / next.md /
rules.md 复制到 workdir/pre/. 不调 pre init, 不写 .claude/settings.json, 不注册
cron — 那些是 U7 install.sh 串起来的活.

调用方:
  - U7 install.sh / pre_update.py — 跑 install_workdir() 后接着调 pre init
  - 单测 — 验证模板字段 + 幂等
"""
from __future__ import annotations

import shutil
from pathlib import Path

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "gover_review"
    / "templates"
)

DEFAULT_WORKDIR = Path.home() / ".pre" / "internal_agents" / "gover_review"


def template_files(templates_dir: Path = TEMPLATES_DIR) -> list[Path]:
    """模板目录内的 .json / .md 文件, 排序确定."""
    if not templates_dir.exists():
        return []
    return sorted(
        p
        for p in templates_dir.iterdir()
        if p.is_file() and p.suffix in (".json", ".md")
    )


def install_workdir(
    workdir: Path,
    *,
    force: bool = False,
    templates_dir: Path = TEMPLATES_DIR,
) -> dict:
    """复制模板到 workdir/pre/.

    布局:
      workdir/
        pre/
          agent_config.json
          next.md
          rules.md
          findings/         (空目录, agent 跑时会写 INFO finding)

    Args:
        workdir: agent cwd (默认 ~/.pre/internal_agents/gover_review)
        force: True 时覆盖已有文件
        templates_dir: 模板源目录 (测试可注入)

    Returns:
        {created: [str], skipped: [str], errors: [str], pre_dir: str}
    """
    created: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    pre_dir = workdir / "pre"
    try:
        pre_dir.mkdir(parents=True, exist_ok=True)
        (pre_dir / "findings").mkdir(exist_ok=True)
    except OSError as e:
        errors.append(f"mkdir {pre_dir}: {e}")
        return {
            "created": created,
            "skipped": skipped,
            "errors": errors,
            "pre_dir": str(pre_dir),
        }

    for tpl in template_files(templates_dir):
        dest = pre_dir / tpl.name
        if dest.exists() and not force:
            skipped.append(str(dest))
            continue
        try:
            shutil.copy2(tpl, dest)
            created.append(str(dest))
        except OSError as e:
            errors.append(f"copy {tpl.name}: {e}")

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "pre_dir": str(pre_dir),
    }
