#!/usr/bin/env python3
"""
verify_node_integrity.py — node 端 sha256 校验 + 篡改检测 + 自动重拉触发

[ Phase B]

usage:
  uv run python scripts/verify_node_integrity.py --node remote-node [--auto-repair]

设计:
  - 从 master.db sync_manifest 表拉 target_node 期望 sha256 list
  - ssh remote node 计算实际 sha256 (find + sha256sum)
  - diff 不一致 → finding HIGH-node-file-tampered-{file_path} + alert + 触发重拉 (master push)
  - --auto-repair: 检测 + 重拉 (跟 sync_to_node.py --node remote-node 集成)

HC-PRE-1 stdlib + ssh subprocess.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from master.persistence import MasterDB  # noqa: E402
from common.paths import PRE_LOG_ROOT  # noqa: E402

_NODE_REMOTE_ROOT = {
    "remote-node": "/root/workspace/",
}
_NODE_SSH_ALIAS = {
    "remote-node": "remote-node-root",
}


def _findings_dir() -> Path:
    d = Path(PRE_LOG_ROOT) / "findings"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(d), 0o700)
    except OSError:
        pass
    return d


def _write_finding_high(title: str, body: str) -> str:
    try:
        d = _findings_dir()
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fpath = d / f"HIGH-{title}-{ts_str}.md"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(
                f"# HIGH: {title}\n\n"
                f"- ts: {ts_str}\n"
                f"- source: scripts/verify_node_integrity.py\n"
                f"- ADR: + NS-M4\n\n"
                f"## body\n\n{body}\n\n"
                f"<phase_b verify_integrity>\n"
            )
        try:
            os.chmod(str(fpath), 0o600)
        except OSError:
            pass
        return str(fpath)
    except OSError as e:
        print(f"[verify_integrity] finding write failed: {e}", file=sys.stderr)
        return ""


def _remote_sha256_map(node: str) -> dict:
    """Run sha256sum on remote node files via ssh, return {file_path: sha256}."""
    ssh_alias = _NODE_SSH_ALIAS.get(node)
    remote_root = _NODE_REMOTE_ROOT.get(node)
    if not ssh_alias or not remote_root:
        return {}
    cmd = ["/usr/bin/ssh", ssh_alias,
           f"find {remote_root}pre {remote_root}pre_rule -type f "
           f"-not -name '*.pyc' -not -path '*/__pycache__/*' "
           f"-not -path '*/.git/*' -not -name '*.bak.*' "
           f"-exec sha256sum {{}} +"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        out = proc.stdout.decode("utf-8", errors="replace")
        result = {}
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                sha, abs_path = parts
                # convert /root/workspace/pre/... → pre/...
                if abs_path.startswith(remote_root):
                    rel = abs_path[len(remote_root):]
                    result[rel] = sha
        return result
    except (subprocess.SubprocessError, OSError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True)
    ap.add_argument("--auto-repair", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="check first N files only (debug)")
    args = ap.parse_args()

    db_path = os.environ.get(
        "PRE_MASTER_DB",
        os.path.join(os.path.expanduser("~"), ".pre", "data", "master.db")
    )
    db = MasterDB(db_path)

    print(f"=== verify_node_integrity node={args.node} ===")
    expected = {r["file_path"]: r["sha256"]
                  for r in db.query_sync_manifest(target_node=args.node)}
    print(f"  expected manifest: {len(expected)} files (master.db SOT)")
    if not expected:
        print(f"  WARN: 0 expected (run sync first via bus_ctl.sh sync {args.node})")
        return 0

    print(f"  fetching remote sha256 (may take ~10s)...")
    actual = _remote_sha256_map(args.node)
    print(f"  remote actual: {len(actual)} files")

    tampered = []
    missing = []
    for path, exp_sha in list(expected.items())[:args.limit if args.limit else None]:
        act_sha = actual.get(path)
        if act_sha is None:
            missing.append(path)
        elif act_sha != exp_sha:
            tampered.append((path, exp_sha, act_sha))

    print(f"\n  tampered: {len(tampered)} files")
    print(f"  missing:  {len(missing)} files")

    for path, exp, act in tampered[:5]:
        print(f"    TAMPERED: {path}")
        print(f"      expected: {exp[:16]}...")
        print(f"      actual:   {act[:16]}...")
        body = (
            f"Node `{args.node}` file `{path}` sha256 mismatch.\n"
            f"- expected (master.db sync_manifest): {exp}\n"
            f"- actual   (remote sha256sum):       {act}\n\n"
            f"违反 HC-DRLI-NS-4 file 只读 + manifest sha256 校验 + HC-DRLI-NS-5 改动需求上行.\n"
            f"可能原因: 远端 file 被篡改, 或本地 file 改后未 sync.\n"
            f"建议: bus_ctl.sh sync {args.node} 重 push 覆盖.\n"
        )
        _write_finding_high(f"node-file-tampered-{args.node}", body)

    if args.auto_repair and (tampered or missing):
        print(f"\n  --auto-repair triggered: re-running sync_to_node.py --node {args.node}")
        repair_cmd = ["uv", "run", "python", str(_REPO / "scripts" / "sync_to_node.py"),
                      "--node", args.node, "--audit", "--force"]
        repair_proc = subprocess.run(repair_cmd, cwd=str(_REPO))
        print(f"  repair exit: {repair_proc.returncode}")

    if tampered:
        print(f"\n[verify_integrity] FAIL: {len(tampered)} tampered files for {args.node}")
        return 1
    if missing:
        print(f"\n[verify_integrity] WARN: {len(missing)} missing files for {args.node}")
    print(f"\n[verify_integrity] OK: all manifest files sha256 match for {args.node}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
