#!/usr/bin/env python3
"""
scripts/agent_reply.py — agent 通过总线回报消息的简易工具.

用法:
  uv run python scripts/agent_reply.py \\
    --to <agent_id> --kind <kind> \\
    --payload '{"verdict":"ok","comment":"..."}'

可选:
  --from <agent_id> 默认从当前 cwd 反推 (例 cwd=$PRE_AGENT_HOME/agent-security
                         → from=local.cli-claude-code-local.agent-security)
  --master <url> 默认 http://127.0.0.1:19500 或 $PRE_MASTER_URL
  --token <secret> Bearer token, 默认 $PRE_SECRET 或 fnpre
  --priority <int> 默认 0
  --parent-id <msg_id> 关联到上游 msg

返回 master 的 JSON 响应 (含 msg_id).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# PR4: token 从 ~/.pre/env 自取 (PRE_HOOK_SECRET); 不再接受 --token / env PRE_SECRET
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from common.token_resolver import resolve as _resolve_token
from common.paths import PRE_AGENT_HOME

# Loopback master call: direct, bypass proxy env (Surge etc.)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def infer_from_agent() -> str:
    """从 cwd 反推 agent_id. cwd 必须在 PRE_AGENT_HOME 下 → local.cli-claude-code-local.<project>."""
    cwd = os.getcwd()
    try:
        rel = os.path.relpath(cwd, PRE_AGENT_HOME)
    except ValueError:
        return ""
    if rel.startswith(".."):
        return ""  # cwd not under PRE_AGENT_HOME
    parts = rel.split(os.sep)
    if not parts or not parts[0] or parts[0].startswith("."):
        return ""
    return f"local.cli-claude-code-local.{parts[0]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="目标 agent_id")
    ap.add_argument("--kind", default="chat",
                    help="消息类型: chat/command/report/verdict_reply/task_verdict 等")
    ap.add_argument("--payload", default="{}", help="JSON 字符串, 会作为 message.payload")
    ap.add_argument("--from", dest="from_agent", default="",
                    help="发送方 agent_id (可省, 默认按 cwd 推断)")
    ap.add_argument("--from-role", default="worker")
    ap.add_argument("--as-user", nargs="?", const="user.default", default=None,
                    metavar="NAME",
                    help="便捷: 以用户身份发 (覆盖 --from / --from-role); "
                         "默认 user.default, 可指定 --as-user user.alice 等; "
                         "from_role 自动设为 user")
    ap.add_argument("--master", default=os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500"))
    ap.add_argument("--priority", type=int, default=0)
    ap.add_argument("--parent-id", default=None)
    args = ap.parse_args()

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"[agent_reply] payload 不是合法 JSON: {e}", file=sys.stderr)
        sys.exit(2)

    body = {
        "kind": args.kind,
        "payload": payload,
        "priority": args.priority,
    }
    if args.as_user:
        body["from_agent"] = args.as_user
        body["from_role"] = "user"
    else:
        from_agent = args.from_agent or infer_from_agent()
        if from_agent:
            body["from_agent"] = from_agent
            body["from_role"] = args.from_role
    if args.parent_id:
        body["parent_id"] = args.parent_id

    url = args.master.rstrip("/") + f"/api/v1/agents/{args.to}/send"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_resolve_token('hook')}",
    }, method="POST")
    try:
        with _NO_PROXY_OPENER.open(req, timeout=10) as resp:
            print(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[agent_reply] HTTP {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(3)
    except urllib.error.URLError as e:
        print(f"[agent_reply] 连不上 master {url}: {e}", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
