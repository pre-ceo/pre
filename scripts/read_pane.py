#!/usr/bin/env python3
"""
scripts/read_pane.py — pre bus read_pane CLI helper.

跟 agent_reply.py 同模式: 调 master REST GET /api/v1/agents/{id}/pane, stdout 输出 sanitized content.

用法:
  uv run python scripts/read_pane.py --agent <agent_id> [--lines N] [--grep PATTERN]
                                       [--master URL] [--token TOK] [--raw]

response 4 status enum:
  ok — pane 抓到, 含 sanitized content
  idle — pane 非空但 agent 处于 idle (启发式简化, 当前不强判)
  empty — pane 抓到但内容空 (cli 启动初期等)
  agent_unavailable — tmux session 不存在 / 远端不可达 / capability 拒
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# PR4: token 从 ~/.pre/env 自取 (PRE_HOOK_SECRET); 不再接受 --token
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from common.token_resolver import resolve as _resolve_token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="目标 agent_id (e.g. local.cli-claude-code-local.pre)")
    ap.add_argument("--lines", type=int, default=200, help="capture lines (max 1000)")
    ap.add_argument("--grep", default="", help="server-side grep filter (净化后)")
    ap.add_argument("--raw", action="store_true",
                    help="raw=true (含 ANSI/sensitive, 必显式 i_understand_risk)")
    ap.add_argument("--master", default=os.environ.get("PRE_MASTER_URL",
                                                            "http://127.0.0.1:19500"))
    ap.add_argument("--json", action="store_true",
                    help="输出原始 JSON (默认仅 content 段)")
    args = ap.parse_args()

    qs = {"lines": str(max(1, min(1000, args.lines)))}
    if args.grep:
        qs["grep"] = args.grep
    if args.raw:
        qs["raw"] = "true"
        qs["i_understand_risk"] = "true"

    url = f"{args.master.rstrip('/')}/api/v1/agents/{args.agent}/pane?" + \
          urllib.parse.urlencode(qs)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {_resolve_token('hook')}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"error": str(e)}
        print(f"[read_pane] HTTP {e.code}: {json.dumps(err_body, ensure_ascii=False)}",
              file=sys.stderr)
        sys.exit(2)
    except (urllib.error.URLError, OSError) as e:
        print(f"[read_pane] master 不可达: {e}", file=sys.stderr)
        sys.exit(3)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    status = data.get("status", "?")
    print(f"[read_pane] status={status} agent={args.agent} "
          f"target_node={data.get('target_node', '?')} "
          f"lines={data.get('line_count_returned', 0)}/{data.get('lines', '?')} "
          f"truncated={data.get('truncated', False)} "
          f"redact_hits={data.get('redacted_patterns_hit', {})}",
          file=sys.stderr)
    if status == "agent_unavailable":
        print(f"[read_pane] agent_unavailable: {data.get('error', '')}",
              file=sys.stderr)
        sys.exit(4)
    print(data.get("content", ""))


if __name__ == "__main__":
    main()
