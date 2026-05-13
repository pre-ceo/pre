#!/usr/bin/env python3
"""
scripts/pre_token.py — bus_tokens 管理 CLI.

用法:
  python3 scripts/pre_token.py issue   --role <node|operator|cli|mcp> --label <name>
                                    [--agent-id <id>] [--expires-days N] [--scopes ...]
  python3 scripts/pre_token.py list    [--all]                # --all 含 revoked
  python3 scripts/pre_token.py revoke  --label <name>
  python3 scripts/pre_token.py rotate  --label <name>         # revoke + issue 同 label
  python3 scripts/pre_token.py show    --label <name>         # 详情 (无 raw)

直接读写 master.db, 不走 master HTTP — admin 操作可能在 master 没起时也要做.
chmod 077 保证新建文件不超 600.

raw token 仅 issue/rotate 时一次性输出. 失踪只能 rotate.
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from master.persistence import MasterDB
from master.auth import hash_token, ROLE_DEFAULT_SCOPES


C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"

DEFAULT_DB = str(Path.home() / ".pre" / "data" / "master.db")


def _open_db(db_path: str) -> MasterDB:
    if not os.path.exists(db_path):
        print(f"{C_YELLOW}[token][warn] {db_path} 不存在, 现在创建{C_RESET}",
              file=sys.stderr)
    return MasterDB(db_path)


def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def cmd_issue(args):
    db = _open_db(args.db)
    role = args.role
    if role not in ROLE_DEFAULT_SCOPES:
        print(f"unknown role: {role}. valid: {sorted(ROLE_DEFAULT_SCOPES)}", file=sys.stderr)
        sys.exit(2)

    if args.scopes:
        scopes = sorted(set(s.strip() for s in args.scopes.split(",") if s.strip()))
    else:
        scopes = sorted(ROLE_DEFAULT_SCOPES[role])

    expires_ts = None
    if args.expires_days:
        expires_ts = time.time() + args.expires_days * 86400.0

    raw = args.raw or secrets.token_hex(16)
    h = hash_token(raw)
    ok = db.insert_bus_token(h, args.label, role, scopes,
                              agent_id=args.agent_id,
                              expires_ts=expires_ts,
                              metadata={"issued_by": "token.py", "issued_ts": time.time()})
    if not ok:
        print(f"{C_YELLOW}[token] label '{args.label}' 已存在 (用 rotate 替换或换 label){C_RESET}",
              file=sys.stderr)
        sys.exit(3)

    print(f"{C_MAGENTA}━━━ token issued ━━━{C_RESET}")
    print(f"  {C_BLUE}label{C_RESET}     {args.label}")
    print(f"  {C_BLUE}role{C_RESET}      {role}")
    print(f"  {C_BLUE}scopes{C_RESET}    {','.join(scopes)}")
    if args.agent_id:
        print(f"  {C_BLUE}agent_id{C_RESET}  {args.agent_id}")
    if expires_ts:
        print(f"  {C_BLUE}expires{C_RESET}   {_fmt_ts(expires_ts)}")
    print()
    print(f"  {C_CYAN}raw token (一次性, 失踪只能 rotate):{C_RESET}")
    print(f"    {raw}")
    print()
    print(f"  {C_DIM}sha256(raw)={h}{C_RESET}")


def cmd_list(args):
    db = _open_db(args.db)
    rows = db.list_bus_tokens(include_revoked=args.all)
    if not rows:
        print("(no tokens)")
        return
    # 表头
    fmt = "{:18s} {:9s} {:38s} {:18s} {:16s} {:16s} {:16s} {:7s}"
    print(fmt.format("LABEL", "ROLE", "SCOPES", "AGENT_ID",
                      "CREATED", "LAST_USED", "EXPIRES", "STATE"))
    print("-" * 165)
    for r in rows:
        scopes_str = ",".join(r["scopes"])[:36]
        if len(",".join(r["scopes"])) > 36:
            scopes_str = scopes_str[:35] + "…"
        agent_id = (r["agent_id"] or "-")[:18]
        state = "revoked" if r["revoked_ts"] else (
            "expired" if r["expires_ts"] and r["expires_ts"] < time.time() else "active")
        print(fmt.format(
            r["label"][:18], r["role"][:9], scopes_str, agent_id,
            _fmt_ts(r["created_ts"]),
            _fmt_ts(r["last_used_ts"]),
            _fmt_ts(r["expires_ts"]),
            state,
        ))


def cmd_revoke(args):
    db = _open_db(args.db)
    ok = db.revoke_bus_token(args.label)
    if ok:
        print(f"{C_CYAN}[token] revoked: {args.label}{C_RESET}")
    else:
        print(f"{C_YELLOW}[token] not found or already revoked: {args.label}{C_RESET}",
              file=sys.stderr)
        sys.exit(3)


def cmd_rotate(args):
    """revoke 旧 label + issue 新 token (同 label_v2). 单一 label 历史保留 audit."""
    db = _open_db(args.db)
    # 找现有
    existing = None
    for r in db.list_bus_tokens(include_revoked=True):
        if r["label"] == args.label and not r["revoked_ts"]:
            existing = r
            break
    if not existing:
        print(f"{C_YELLOW}[token] no active token labeled '{args.label}'{C_RESET}",
              file=sys.stderr)
        sys.exit(3)

    # revoke old
    db.revoke_bus_token(args.label)

    # issue new — 复用 role / scopes / agent_id / expires
    role = existing["role"]
    raw = secrets.token_hex(16)
    h = hash_token(raw)
    expires = existing.get("expires_ts")
    new_label = args.label  # 旧已 revoked, 复用 label 不冲突? 实际 UNIQUE 仍冲突 —
                              # 但因 revoked_ts 不影响 UNIQUE, label 仍占着. 改为 _v2 风格.
    new_label = f"{args.label}.{int(time.time())}"
    db.insert_bus_token(h, new_label, role, existing["scopes"],
                         agent_id=existing.get("agent_id"),
                         expires_ts=expires,
                         metadata={"rotated_from": args.label,
                                    "rotated_ts": time.time()})
    print(f"{C_MAGENTA}━━━ token rotated ━━━{C_RESET}")
    print(f"  {C_BLUE}old label{C_RESET}  {args.label}  (revoked)")
    print(f"  {C_BLUE}new label{C_RESET}  {new_label}")
    print(f"  {C_BLUE}role{C_RESET}       {role}")
    print()
    print(f"  {C_CYAN}new raw token (一次性):{C_RESET}")
    print(f"    {raw}")

    # role=gui — 同时输出 fe ui 一次性激活 magic link (跟 start_master bootstrap 一致)
    if role == "gui":
        base = os.environ.get("FNPRE_UI_URL", "http://127.0.0.1:5174/index.html")
        link = f"{base}#token={raw}&next=/"
        print()
        print(f"{C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{C_RESET}")
        print(f"{C_CYAN}  FE UI 一次性激活链接 (浏览器打开 → 自动保存 token → 跳 /):{C_RESET}")
        print(f"{C_CYAN}  {link}{C_RESET}")
        print(f"{C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{C_RESET}")


def cmd_show(args):
    db = _open_db(args.db)
    target = None
    for r in db.list_bus_tokens(include_revoked=True):
        if r["label"] == args.label:
            target = r
            break
    if not target:
        print(f"not found: {args.label}", file=sys.stderr)
        sys.exit(3)
    print(json.dumps(target, indent=2, default=str, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(
        description="bus_tokens 管理 CLI (multi-token RBAC)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default=DEFAULT_DB, help=f"sqlite 路径 (默认 {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_i = sub.add_parser("issue", help="发新 token")
    sp_i.add_argument("--role", required=True, choices=sorted(ROLE_DEFAULT_SCOPES.keys()))
    sp_i.add_argument("--label", required=True, help="人类可读 label, 唯一")
    sp_i.add_argument("--agent-id", help="(mcp role) 锁定 token 仅可代表的 agent_id")
    sp_i.add_argument("--expires-days", type=int, help="N 天后过期, 不传=永久")
    sp_i.add_argument("--scopes", help="逗号分隔覆盖默认 scopes (默认按 role 自动)")
    sp_i.add_argument("--raw", help="(测试) 指定 raw token, 而非随机生成")
    sp_i.set_defaults(func=cmd_issue)

    sp_l = sub.add_parser("list", help="列出 token")
    sp_l.add_argument("--all", action="store_true", help="含 revoked")
    sp_l.set_defaults(func=cmd_list)

    sp_r = sub.add_parser("revoke", help="软删 token")
    sp_r.add_argument("--label", required=True)
    sp_r.set_defaults(func=cmd_revoke)

    sp_o = sub.add_parser("rotate", help="撤旧 + 同配置发新")
    sp_o.add_argument("--label", required=True)
    sp_o.set_defaults(func=cmd_rotate)

    sp_s = sub.add_parser("show", help="单 token 详情 (不含 raw)")
    sp_s.add_argument("--label", required=True)
    sp_s.set_defaults(func=cmd_show)

    args = p.parse_args()
    os.umask(0o077)
    args.func(args)


if __name__ == "__main__":
    main()
