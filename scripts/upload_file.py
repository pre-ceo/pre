#!/usr/bin/env python3
"""
pre/scripts/upload_file.py — agent 用 helper, 上传文件给 master 跨 node 共享

用法:
  uv run python scripts/upload_file.py <local_path> [--recipient <agent_id>] \\
                                        [--from <agent_id>] [--name <name>]
  → stdout 输出 file_id (单行)

例:
  file_id=$(uv run python scripts/upload_file.py /tmp/big_log.txt \\
              --recipient remote-node.cli-claude-code-local.agent-research \\
              --from local.cli-claude-code-local.pre)
  echo "file_id=$file_id"

后续 chat 时把 file_id 放进 payload.attachments=[{file_id, name, size, sha256}]:
  agent_reply.py --to ... --payload '{"text":"...","attachments":[{"file_id":"<id>","name":"big_log.txt"}]}'
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# PR4: token 从 ~/.pre/env 自取 (PRE_HOOK_SECRET); 不再接受 --secret
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from common.token_resolver import resolve as _resolve_token


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path", help="local file path to upload")
    p.add_argument("--recipient", default="", help="target agent_id (ACL)")
    p.add_argument("--from", dest="from_agent", default="user.tmux", help="uploader agent_id")
    p.add_argument("--name", default="", help="display name (default: basename)")
    p.add_argument("--master", default=os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500"))
    args = p.parse_args()

    if not os.path.isfile(args.path):
        print(f"file not found: {args.path}", file=sys.stderr)
        sys.exit(1)
    name = args.name or os.path.basename(args.path)
    with open(args.path, "rb") as f:
        data = f.read()

    if len(data) > 16 * 1024 * 1024:
        print(f"file too large: {len(data)} > 16MB", file=sys.stderr)
        sys.exit(2)

    req = urllib.request.Request(
        args.master.rstrip("/") + "/api/v1/files/upload",
        data=data,
        headers={
            "Authorization": f"Bearer {_resolve_token('hook')}",
            "Content-Type": "application/octet-stream",
            "X-Agent-Id": args.from_agent,
            "X-Recipient": args.recipient,
            "X-File-Name": name,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"upload failed: {e.code} {e.read().decode()[:300]}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"upload failed: {e}", file=sys.stderr)
        sys.exit(3)

    if not resp.get("ok"):
        print(f"upload error: {resp}", file=sys.stderr)
        sys.exit(4)

    print(resp["file_id"])  # 单行 file_id, 方便 shell capture


if __name__ == "__main__":
    main()
