"""audit — pre_mcp tool call audit jsonl (复用 master.redact.safe_audit_dump).

路径: ${PRE_LOG_ROOT}/mcp_audit/{node_id}_{date}.jsonl chmod 600 (dir 700).
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 加入 src/ 到 sys.path 让 master.redact import (跟 pre_mcp 子进程独立 lifecycle)
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

try:
    from master.redact import safe_audit_dump  # noqa: E402
except ImportError:
    safe_audit_dump = None  # fallback


def _audit_dir() -> Path:
    # PRE_LOG_DIR 由 pre_mcp/__main__.py 启动时 source ~/.pre/env 注入;
    # 测试 / 独立 import 时 fallback 到 sibling 推算.
    base = Path(os.environ.get(
        "PRE_LOG_DIR",
        str(_REPO_ROOT.parent / "pre_log"),
    )) / "mcp_audit"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(base), 0o700)
    except OSError:
        pass
    return base


def write_audit(entry: dict, node_id: str = "local") -> bool:
    """写一条 audit. 用 master.redact.safe_audit_dump 脱敏 (M1 spec A 复用)."""
    try:
        d = _audit_dir()
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        fpath = d / f"{node_id}_{date_str}.jsonl"
        is_new = not fpath.exists()
        if safe_audit_dump:
            line = safe_audit_dump(entry)
        else:
            line = json.dumps(entry, ensure_ascii=False)
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if is_new:
            try:
                os.chmod(str(fpath), 0o600)
            except OSError:
                pass
        return True
    except OSError:
        return False
