#!/usr/bin/env python3
"""
scripts/dispatch_inbox.py — 拉某个 agent 的 inbox (master 上 to_agent=该 agent 的消息).

默认拉 fn_dispatcher 的 inbox.

用法:
  uv run python scripts/dispatch_inbox.py
  uv run python scripts/dispatch_inbox.py --agent-id local.cli-claude-code-local.fn_dispatcher
  uv run python scripts/dispatch_inbox.py --since 1777354222.5 --limit 50
  uv run python scripts/dispatch_inbox.py --kind verdict_reply

输出: 表格形式列消息, 详情用 --raw 输出原始 JSON.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# PR4: token 从 ~/.pre/env 自取 (PRE_HOOK_SECRET); 不再接受 --token
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from common.token_resolver import resolve as _resolve_token


DEFAULT_AGENT = "local.cli-claude-code-local.fn_dispatcher"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", default=DEFAULT_AGENT)
    ap.add_argument("--master", default=os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500"))
    ap.add_argument("--since", type=float, default=0)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--kind", default="", help="只显示指定 kind")
    ap.add_argument("--raw", action="store_true", help="输出原始 JSON")
    args = ap.parse_args()

    url = (args.master.rstrip("/") +
           f"/api/v1/agents/{args.agent_id}/messages?since={args.since}&limit={args.limit}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_resolve_token('hook')}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[inbox] 连不上 master: {e}", file=sys.stderr)
        sys.exit(4)

    msgs = data.get("messages", [])
    if args.kind:
        msgs = [m for m in msgs if m.get("kind") == args.kind]

    if args.raw:
        print(json.dumps(msgs, indent=2, ensure_ascii=False))
        return

    if not msgs:
        print(f"[inbox] {args.agent_id} 没有新消息 (since={args.since})")
        return

    # 表格 (英文表头, 等宽对齐)
    print(f"{'TS':<22} {'KIND':<16} {'FROM':<48} {'MSG_ID':<12} PAYLOAD_PREVIEW")
    for m in msgs:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("ts", 0)))
        kind = m.get("kind", "")[:16]
        frm = (m.get("from_agent") or "?")[:48]
        mid = (m.get("id") or "")[:12]
        payload = m.get("payload", {})
        if isinstance(payload, dict):
            preview = json.dumps(payload, ensure_ascii=False)[:80]
        else:
            preview = str(payload)[:80]
        print(f"{ts:<22} {kind:<16} {frm:<48} {mid:<12} {preview}")
    print(f"\n表头: TS=时间 / KIND=类型 / FROM=发送方 / MSG_ID=消息ID / PAYLOAD_PREVIEW=载荷预览")


if __name__ == "__main__":
    main()
