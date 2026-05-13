#!/usr/bin/env python3
"""
scripts/decide.py — 远程给 agent 的 ask UI 注入按键 (替代人类操作).

用法:
  uv run python scripts/decide.py --to <agent_id> --key 1 # 选择第 1 项
  uv run python scripts/decide.py --to <agent_id> --key 2
  uv run python scripts/decide.py --to <agent_id> --key Escape
  uv run python scripts/decide.py --to <agent_id> --key 3 # 拒绝

key 直接传给 tmux send-keys, 支持 1/2/3/Escape/Up/Down/Enter 等 tmux key 名.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# PR4: token 从 ~/.pre/env 自取 (PRE_HOOK_SECRET); 不再接受 --token
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from common.token_resolver import resolve as _resolve_token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="目标 agent_id")
    ap.add_argument("--key", required=True, help="按键: 1/2/3/Escape/Up/Down/Enter")
    ap.add_argument("--by", default="cli.decide", help="发起者标识 (审计用)")
    ap.add_argument("--master", default=os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500"))
    args = ap.parse_args()

    url = args.master.rstrip("/") + f"/api/v1/agents/{args.to}/decide"
    body = {"key": args.key, "by_agent": args.by}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_resolve_token('hook')}",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[decide] HTTP {e.code}: {e.read().decode('utf-8','replace')}", file=sys.stderr)
        sys.exit(3)
    except urllib.error.URLError as e:
        print(f"[decide] 连不上 master: {e}", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
