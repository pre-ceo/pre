#!/usr/bin/env python3
"""install_mcp_registration.py — 在 ~/.claude.json 注册 pre 的 MCP server.

调用方: scripts/install.sh.

行为:
- 读 ~/.claude.json (不存在则用 {}).
- 检查 mcpServers.pre 块.
  - 不存在 → 写入新模板 (command/args/env 用 $PRE_ROOT 实值).
  - 存在但 command/args/env 跟模板差异 → backup ~/.claude.json + 覆盖, 打印 diff 摘要.
  - 一致 → 跳过.
- 不改 mcpServers 之外的任何 key (theme / model / 其它 mcpServers 等).

token 不写进 ~/.claude.json — pre_mcp 子进程自己读 ~/.pre/env 拿 PRE_MCP_SECRET.

用法:
    python3 install_mcp_registration.py --pre-root <abs> [--claude-json PATH] [-y]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _template(pre_root: str) -> dict:
    """生成 mcpServers.pre 的标准注册块.

    用 ~/.local/bin/pre-mcp shim. shim 自己 source ~/.pre/env 注入
    PRE_MCP_SECRET + PRE_ROOT, 不写 raw token 进 ~/.claude.json. Token
    轮换 = 改 ~/.pre/env 一处, 不动 claude/codex/gemini config.
    """
    shim = os.path.join(os.path.expanduser("~"), ".local", "bin", "pre-mcp")
    return {
        "command": shim,
        "args": [],
        "env": {
            "PRE_MASTER_URL": "http://127.0.0.1:19500",
            "PRE_NODE_ID": "local",
        },
    }


def _normalize(obj) -> str:
    """canonical JSON for comparison (sorted keys, fixed indent)."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False)


def install(claude_json: Path, pre_root: str) -> int:
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                print(f"FATAL: {claude_json} root is not a JSON object", file=sys.stderr)
                return 1
        except (OSError, json.JSONDecodeError) as e:
            print(f"FATAL: cannot read {claude_json}: {e}", file=sys.stderr)
            return 1
    else:
        data = {}

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(f"FATAL: mcpServers in {claude_json} is not an object", file=sys.stderr)
        return 1

    template = _template(pre_root)
    existing = servers.get("pre")

    action = ""
    if existing is None:
        servers["pre"] = template
        action = "added"
    elif not isinstance(existing, dict):
        print(f"WARNING: mcpServers.pre is not an object — overwriting")
        servers["pre"] = template
        action = "overwritten"
    else:
        # 只比对 command/args, env 允许 user 加额外 key (e.g. PRE_CALLER_AGENT_ID
        # 项目特定). 但 PRE_MASTER_URL / PRE_NODE_ID 若被改成了非 template 值,
        # 视为 user 自定义, 不覆盖.
        cmd_ok = existing.get("command") == template["command"]
        args_ok = existing.get("args") == template["args"]
        if cmd_ok and args_ok:
            # 合并 env: template 提供的 key 若 user 没设, 补上; user 已设的不动.
            user_env = existing.get("env")
            if not isinstance(user_env, dict):
                user_env = {}
            merged_env = dict(template["env"])
            merged_env.update(user_env)  # user 优先
            if _normalize(existing.get("env") or {}) == _normalize(merged_env):
                action = "unchanged"
            else:
                existing["env"] = merged_env
                action = "env-merged"
        else:
            # command/args 漂移 → backup + 覆盖
            backup = claude_json.with_suffix(claude_json.suffix + f".bak.{_ts()}")
            shutil.copy2(claude_json, backup)
            print(f"  mcpServers.pre command/args differ from template — overwriting")
            print(f"  backup: {backup}")
            print("  --- old ---")
            print(_normalize(existing))
            print("  --- new ---")
            print(_normalize(template))
            servers["pre"] = template
            action = "overwritten"

    if action != "unchanged":
        claude_json.parent.mkdir(parents=True, exist_ok=True)
        tmp = claude_json.with_suffix(claude_json.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(claude_json)

    print(f"\n✓ MCP registration {action}: mcpServers.pre in {claude_json}")
    print("  PRE_MCP_SECRET is loaded by the pre_mcp subprocess from ~/.pre/env")
    print("  per-project PRE_CALLER_AGENT_ID can be set in your project's "
          ".claude/settings.json env block (overrides this default).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pre-root", required=True,
                   help="absolute path to pre repo (used in mcpServers.pre.args)")
    p.add_argument("--claude-json", default=None,
                   help="path to claude.json (default: ~/.claude.json)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="non-interactive (reserved; current behavior is idempotent)")
    args = p.parse_args(argv)

    pre_root = str(Path(args.pre_root).expanduser().resolve())
    claude_json = Path(args.claude_json or os.path.expanduser("~/.claude.json"))

    return install(claude_json, pre_root)


if __name__ == "__main__":
    sys.exit(main())
