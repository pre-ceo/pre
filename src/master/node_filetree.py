"""
node_filetree.py — node 需要的文件树 SoT (Source of Truth)

[取代 sync_to_node.py 散在代码里的 include/exclude]

设计原则:
  - master push only (HC-DRLI-), 单向 DAG
  - 结构化 sections: code / config / runtime, 各自独立 include 集
  - exclude 通用一份, 全 section 共享
  - rsync include/exclude 顺序敏感: 长 include 优先, 通配最后
  - 显式定义 cwd_root (PRE_AGENT_HOME), 所有 path 相对它

usage:
    from master.node_filetree import build_rsync_filters, list_files
    args = build_rsync_filters(sections=["code", "config"])
    # ['--include=/pre/', '--include=/pre/scripts/***', ...]
    files = list_files(sections=["code"])
    # [Path('pre/scripts/sync_to_node.py'), ...]

HC-PRE-1 stdlib only.
"""
from __future__ import annotations
from pathlib import Path
from common.paths import PRE_AGENT_HOME
from typing import Iterable


CWD_ROOT = Path(PRE_AGENT_HOME)


SECTIONS: dict[str, dict] = {
    "code": {
        "_doc": "pre 仓库代码 + 顶层配置 (随 commit 变, 业务运行不可少)",
        "includes": [
            "/pre/",
            "/pre/scripts/***",
            "/pre/src/***",
            "/pre/pre_mcp/***",
            "/pre/pre/***",
            "/pre/docs/***",
            "/pre/dev-workflow/***",
            "/pre/CLAUDE.md",
            "/pre/README.md",
            "/pre/.gitignore",
            "/pre/pyproject.toml",
            "/pre/uv.lock",
            "/pre/.python-version",
        ],
    },
    "config": {
        "_doc": "pre_rule 用户级配置/规则 (跨节点共享, 不含 runtime 状态)",
        "includes": [
            "/pre_rule/",
            "/pre_rule/*.md",
            "/pre_rule/*.json",
            "/pre_rule/cron/***",
            "/pre_rule/freerun/***",
            "/pre_rule/hook/***",
            "/pre_rule/agents/***",
            "/pre_rule/.env_sync_secret",
            "/pre_rule/tmux_startup.sh",
        ],
    },
    "runtime": {
        "_doc": "pre_rule 运行时状态 (race 敏感, 谨慎同步; 默认归 'all' 但单 section 可关)",
        "includes": [
            "/pre_rule/runtime/***",
            "/pre_rule/logs/***",
        ],
    },
}


# 全 section 共享 exclude (顺序无关, rsync 平铺)
COMMON_EXCLUDES: list[str] = [
    "*.pyc",
    "__pycache__",
    ".venv",
    ".git",
    "*.db",
    "*.bak.*",
    "*.tmp",
    "/pre_log/***",
    "/pre/master.db",
    "/pre/logs/***",
    "/pre/agents/***",
    "/.pre/***",
]


# 默认全开 (兼容现有行为, --section all 等价此)
DEFAULT_SECTIONS: list[str] = ["code", "config", "runtime"]


def build_rsync_filters(sections: Iterable[str] | None = None) -> list[str]:
    """生成 rsync --include/--exclude 列表.

    顺序: includes (按 section 顺序) → COMMON_EXCLUDES → 通配收尾 (--include=*/ --exclude=*).
    """
    secs = list(sections) if sections else DEFAULT_SECTIONS
    args: list[str] = []
    for sec in secs:
        spec = SECTIONS.get(sec)
        if not spec:
            raise ValueError(f"unknown section: {sec!r} (valid: {list(SECTIONS)})")
        for pat in spec["includes"]:
            args.append(f"--include={pat}")
    for pat in COMMON_EXCLUDES:
        args.append(f"--exclude={pat}")
    # 收尾: 允许目录递归, 排除其他所有
    args.append("--include=*/")
    args.append("--exclude=*")
    return args


def list_files(sections: Iterable[str] | None = None,
               cwd_root: Path | None = None) -> list[Path]:
    """扫源端 cwd_root 下匹配 sections 的实际文件 (用于生成 manifest hash 入库).

    返回相对 cwd_root 的 PosixPath 列表. 跳过 exclude.
    """
    secs = list(sections) if sections else DEFAULT_SECTIONS
    root = cwd_root or CWD_ROOT
    # 提取 section 顶层目录 (e.g. /pre/scripts/*** → pre/scripts)
    # 单文件 pattern (e.g. /pre/CLAUDE.md) 单独存
    dir_roots: list[Path] = []
    explicit_files: list[Path] = []
    for sec in secs:
        spec = SECTIONS.get(sec)
        if not spec:
            raise ValueError(f"unknown section: {sec!r}")
        for pat in spec["includes"]:
            p = pat.lstrip("/")
            if p.endswith("/***"):
                dir_roots.append(root / p[:-4])
            elif p.endswith("/"):
                continue  # 仅占位的目录 include
            elif "*" in p:
                # /pre_rule/*.md style — glob 解析
                parent = root / p.rsplit("/", 1)[0]
                if parent.is_dir():
                    name_glob = p.rsplit("/", 1)[1]
                    for f in parent.glob(name_glob):
                        if f.is_file():
                            explicit_files.append(f)
            else:
                f = root / p
                if f.is_file():
                    explicit_files.append(f)

    out: list[Path] = []
    skip_segments = ("__pycache__", ".venv", ".git/", "/.git", "_legacy_")
    for base in dir_roots:
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(root).as_posix()
            if any(seg in rel for seg in skip_segments):
                continue
            if rel.endswith(".pyc") or ".bak." in rel:
                continue
            if rel.endswith(".db") or rel == "pre/master.db":
                continue
            if rel.endswith(".tmp"):
                continue
            out.append(f.relative_to(root))
    for f in explicit_files:
        rel = f.relative_to(root)
        out.append(rel)
    # dedup 保序
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        s = p.as_posix()
        if s not in seen:
            seen.add(s)
            unique.append(p)
    return unique


def section_summary() -> str:
    """human-readable 文件树摘要 (CLI --list-tree 用)."""
    lines = ["=== node filetree (sections) ==="]
    for sec, spec in SECTIONS.items():
        lines.append(f"\n[{sec}]  {spec.get('_doc', '')}")
        for pat in spec["includes"]:
            lines.append(f"  include: {pat}")
    lines.append("\n[common excludes]")
    for pat in COMMON_EXCLUDES:
        lines.append(f"  exclude: {pat}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        files = list_files()
        for f in files:
            print(f.as_posix())
        print(f"\ntotal: {len(files)} files", file=sys.stderr)
    else:
        print(section_summary())
