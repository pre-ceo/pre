#!/usr/bin/env python3
"""
pre/scripts/download_file.py — agent 用 helper, 从 master 拉跨 node 文件

用法:
  uv run python scripts/download_file.py <file_id> --as <agent_id> --out <local_path>

例 (remote-node agent-research 收到 chat 含 attachment file_id 后):
  uv run python scripts/download_file.py db44ee09543f42a7 \\
      --as remote-node.cli-claude-code-local.agent-research \\
      --out /tmp/received.txt
"""
from __future__ import annotations
import argparse
import os
import sys
import urllib.error
import urllib.request

# PR4: token 从 ~/.pre/env 自取 (PRE_HOOK_SECRET); 不再接受 --secret
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from common.token_resolver import resolve as _resolve_token


def main():
    p = argparse.ArgumentParser()
    p.add_argument("file_id")
    p.add_argument("--as", dest="as_agent", required=True, help="requester agent_id (ACL)")
    p.add_argument("--out", required=True, help="local path to write")
    p.add_argument("--master", default=os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500"))
    args = p.parse_args()

    req = urllib.request.Request(
        args.master.rstrip("/") + f"/api/v1/files/{args.file_id}",
        headers={
            "Authorization": f"Bearer {_resolve_token('hook')}",
            "X-Agent-Id": args.as_agent,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        print(f"download failed: {e.code} {e.read().decode()[:300]}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"download failed: {e}", file=sys.stderr)
        sys.exit(3)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(data)
    print(f"wrote {len(data)} bytes → {args.out}")


if __name__ == "__main__":
    main()
