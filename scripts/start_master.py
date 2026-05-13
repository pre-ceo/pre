#!/usr/bin/env python3
"""
启动 pre Master.

用法:
  uv run python scripts/start_master.py [--host 127.0.0.1] [--port 19500]

multi-token RBAC: 不再接受 --secret. token 全在 master.db 的 bus_tokens 表.
首次启动 (空表) 自动建 6 个默认 token (node/operator/cli/mcp/gui/hook), raw 写到
~/.pre/data/initial_tokens.txt chmod 600. 读了 → 写到 ~/.pre/env 对应
PRE_<KIND>_SECRET → rm 本文件.

旧 PRE_SECRET / PRE_NODE_SECRET env 一律忽略.
"""
import argparse
import asyncio
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from master.server import run_master, DEFAULT_HOST, DEFAULT_PORT, DEFAULT_DB
from master.persistence import MasterDB
from master.auth import hash_token, ROLE_DEFAULT_SCOPES


C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_RESET = "\033[0m"


def _bootstrap_tokens(db: MasterDB, db_path: str) -> list[tuple[str, str]]:
    """空表时建 4 个默认 token, 返 [(label, raw)] 列表 给 stdout/file 输出."""
    if db.count_active_bus_tokens() > 0:
        return []  # 非空, 不 bootstrap

    issued: list[tuple[str, str]] = []
    defaults = [
        ("node-default",     "node",     None),
        ("operator-default", "operator", None),
        ("cli-default",      "cli",      None),
        # mcp-default 不绑 agent_id (deploy-time 用 token issue --agent-id 再发针对性)
        ("mcp-default",      "mcp",      None),
        # gui-default → pre_ui browser, master /auth/init 颁发后落 browser
        ("gui-default",      "gui",      None),
        # hook-default → hook/runtime 模块走本机 loopback 调 master HTTP
        ("hook-default",     "hook",     None),
    ]
    for label, role, agent_id in defaults:
        raw = secrets.token_hex(16)
        scopes = sorted(ROLE_DEFAULT_SCOPES[role])
        ok = db.insert_bus_token(hash_token(raw), label, role, scopes,
                                  agent_id=agent_id)
        if ok:
            issued.append((label, raw))

    if not issued:
        return []

    # 写到 initial_tokens.txt chmod 600
    init_file = Path(db_path).parent / "initial_tokens.txt"
    lines = [
        "# pre master initial bus_tokens — DELETE AFTER YOU CONFIGURED CLIENTS",
        "# 6 default tokens generated on first master start (node/operator/cli/mcp/gui/hook).",
        "# raw 写到 ~/.pre/env 对应 PRE_<KIND>_SECRET 后, rm 本文件.",
        "# Use scripts/pre_token.py issue ... 给特定 agent / 来源发更细粒度 token.",
        "",
    ]
    for label, raw in issued:
        lines.append(f"{label}={raw}")
    init_file.parent.mkdir(parents=True, exist_ok=True)
    init_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(init_file, 0o600)
    except OSError:
        pass
    return issued


def main():
    # umask 077: 新建文件默认 600 (防止 master.db / initial_tokens 被世界可读)
    os.umask(0o077)

    p = argparse.ArgumentParser()
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--db", default=DEFAULT_DB, help="sqlite 路径")
    args = p.parse_args()

    # bind 校验 — 默认只允许 127.0.0.1, PRE_ALLOW_PUBLIC_BIND=1 绕过
    allow_public = os.environ.get("PRE_ALLOW_PUBLIC_BIND", "") == "1"
    if not allow_public and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[master] refusing to bind {args.host}: not loopback. "
              f"set PRE_ALLOW_PUBLIC_BIND=1 to override", file=sys.stderr)
        sys.exit(2)

    # 警告 legacy env (清破式升级, 现在无效)
    legacy_env = (os.environ.get("PRE_SECRET")
                  or os.environ.get("PRE_NODE_SECRET")
                  or os.environ.get("PRE_SECRET_LEGACY"))
    if legacy_env:
        print(f"{C_YELLOW}[master][warn] legacy env (PRE_SECRET / "
              f"PRE_NODE_SECRET / PRE_SECRET_LEGACY) set but IGNORED — "
              f"multi-token RBAC 已上线, 用 scripts/pre_token.py 管理 token{C_RESET}",
              file=sys.stderr)

    # bootstrap (空表时)
    db = MasterDB(args.db)
    issued = _bootstrap_tokens(db, args.db)
    if issued:
        init_file = str(Path(args.db).parent / "initial_tokens.txt")
        print(f"{C_MAGENTA}[master] ━━━ first start: 4 default tokens issued ━━━{C_RESET}",
              file=sys.stderr)
        print(f"{C_MAGENTA}[master] raw tokens 写到 {init_file} (chmod 600){C_RESET}",
              file=sys.stderr)
        print(f"{C_MAGENTA}[master] 读完配置完客户端 → rm 这个文件{C_RESET}",
              file=sys.stderr)
        for label, raw in issued:
            print(f"{C_BLUE}[master]   {label:18s}  {raw}{C_RESET}", file=sys.stderr)
        print(f"{C_CYAN}[master] 后续管理用 `python3 scripts/pre_token.py issue/list/revoke/rotate`{C_RESET}",
              file=sys.stderr)
    db.conn.close()  # 让 run_master 自己重新打开 (避免共享 connection)

    # secret 参数已废弃, 传空字符串 (server.py 里 _check_auth 不读)
    try:
        asyncio.run(run_master(args.host, args.port, args.db, secret=""))
    except KeyboardInterrupt:
        print("\n[master] stopped.")


if __name__ == "__main__":
    main()
