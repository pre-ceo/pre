#!/usr/bin/env python3
"""
启动 pre Master.

用法:
  uv run python scripts/start_master.py [--host 127.0.0.1] [--port 19500]

multi-token RBAC: 不再接受 --secret. token 全在 master.db 的 bus_tokens 表.
启动时 idempotent bootstrap: 检查 6 个 default label (node/operator/cli/mcp/gui/hook),
缺哪个 label 补哪个. 新颁发的 raw 自动:
  - append 到 ~/.pre/data/initial_tokens.txt (chmod 600)
  - append 到 ~/.pre/env 对应 PRE_<KIND>_SECRET (if-not-set; chmod 600)
  - gui-default 同时 stderr 输出 fe ui 一次性激活 magic link (token 走 URL fragment)

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


# label → ~/.pre/env key (跟 src/common/token_resolver.py:_KIND_TO_ENV_KEY 对齐)
# cli-default 不写 env (PR4 后 deprecated, hook 替代)
_LABEL_TO_ENV_KEY: dict[str, str] = {
    "node-default":     "PRE_NODE_SECRET",
    "operator-default": "PRE_OPERATOR_SECRET",
    "mcp-default":      "PRE_MCP_SECRET",
    "gui-default":      "PRE_GUI_SECRET",
    "hook-default":     "PRE_HOOK_SECRET",
}


def _magic_link(gui_raw: str) -> str:
    """生成 fe ui 一次性激活 URL (token 走 fragment, 不进 server log)."""
    base = os.environ.get("FNPRE_UI_URL", "http://127.0.0.1:5174/index.html")
    return f"{base}#token={gui_raw}&next=/"


def _write_secrets_to_env(issued: list[tuple[str, str]]) -> list[str]:
    """新颁发的 token 写到 ~/.pre/env. 已设的 KEY 不覆盖. 返写入的 env key 列表."""
    env_file = Path.home() / ".pre" / "env"
    env_file.parent.mkdir(parents=True, exist_ok=True)

    existing_text = ""
    if env_file.exists():
        try:
            existing_text = env_file.read_text(encoding="utf-8")
        except OSError:
            existing_text = ""

    existing_lines = existing_text.split("\n")
    appended_keys: list[str] = []
    appends: list[str] = []
    for label, raw in issued:
        env_key = _LABEL_TO_ENV_KEY.get(label)
        if not env_key:
            continue  # cli-default 跳过
        # 检测 KEY= line start (允许前置空白)
        if any(line.lstrip().startswith(f"{env_key}=") for line in existing_lines):
            continue
        appends.append(f"{env_key}={raw}  # auto-set by start_master ({label})")
        appended_keys.append(env_key)

    if not appends:
        return []

    with open(env_file, "a", encoding="utf-8") as f:
        if not existing_text.endswith("\n") and existing_text:
            f.write("\n")
        f.write("# auto-injected default tokens (bootstrap idempotent)\n")
        f.write("\n".join(appends) + "\n")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    return appended_keys


def _bootstrap_tokens(db: MasterDB, db_path: str) -> list[tuple[str, str]]:
    """Idempotent bootstrap: 检查 6 个 default label 在不在 db, 缺哪个补哪个.

    返本次新颁发的 [(label, raw)] 列表 (空 = 全 6 都已存在).
    新 raw 写到 initial_tokens.txt (append) + ~/.pre/env (if-key-not-set).
    """
    defaults = [
        ("node-default",     "node",     None),
        ("operator-default", "operator", None),
        ("cli-default",      "cli",      None),
        # mcp-default 不绑 agent_id (deploy-time 用 token issue --agent-id 再发针对性)
        ("mcp-default",      "mcp",      None),
        # gui-default → pre_ui browser, master 颁发后通过 magic link 落 localStorage
        ("gui-default",      "gui",      None),
        # hook-default → hook/runtime 模块走本机 loopback 调 master HTTP
        ("hook-default",     "hook",     None),
    ]

    # 已有 labels (含 revoked — 不重发已撤的 label 防冲突, 必要时 admin 用 rotate)
    existing_labels = {t["label"] for t in db.list_bus_tokens(include_revoked=True)}

    issued: list[tuple[str, str]] = []
    for label, role, agent_id in defaults:
        if label in existing_labels:
            continue
        raw = secrets.token_hex(16)
        scopes = sorted(ROLE_DEFAULT_SCOPES[role])
        ok = db.insert_bus_token(hash_token(raw), label, role, scopes,
                                  agent_id=agent_id)
        if ok:
            issued.append((label, raw))

    if not issued:
        return []

    # 追加 initial_tokens.txt (append-or-create, idempotent — 重启只 append 新 label)
    init_file = Path(db_path).parent / "initial_tokens.txt"
    init_file.parent.mkdir(parents=True, exist_ok=True)
    is_new = not init_file.exists()
    with open(init_file, "a", encoding="utf-8") as f:
        if is_new:
            f.write(
                "# pre master initial bus_tokens — DELETE AFTER YOU CONFIGURED CLIENTS\n"
                "# Idempotent: master 启动检查缺失 default label 并补齐.\n"
                "# 启动还会 append 到 ~/.pre/env 对应 PRE_<KIND>_SECRET (if-not-set).\n"
                "# Use scripts/pre_token.py issue ... 给特定 agent / 来源发更细粒度 token.\n\n"
            )
        for label, raw in issued:
            f.write(f"{label}={raw}\n")
    try:
        os.chmod(init_file, 0o600)
    except OSError:
        pass

    # 回写 ~/.pre/env (if-key-not-set)
    _write_secrets_to_env(issued)

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

    # bootstrap (idempotent — 缺哪个 default label 补哪个)
    db = MasterDB(args.db)
    issued = _bootstrap_tokens(db, args.db)
    if issued:
        init_file = str(Path(args.db).parent / "initial_tokens.txt")
        env_file = str(Path.home() / ".pre" / "env")
        print(f"{C_MAGENTA}[master] ━━━ bootstrap: {len(issued)} new default token(s) issued ━━━{C_RESET}",
              file=sys.stderr)
        print(f"{C_MAGENTA}[master] appended to {init_file} (chmod 600){C_RESET}",
              file=sys.stderr)
        print(f"{C_MAGENTA}[master] appended to {env_file} as PRE_<KIND>_SECRET (if-not-set){C_RESET}",
              file=sys.stderr)
        for label, raw in issued:
            print(f"{C_BLUE}[master]   {label:18s}  {raw}{C_RESET}", file=sys.stderr)

        # GUI token magic link — fragment 不进 server log, fe ui 入口自动 localStorage 落地
        gui_raw = next((raw for lb, raw in issued if lb == "gui-default"), None)
        if gui_raw:
            link = _magic_link(gui_raw)
            print(file=sys.stderr)
            print(f"{C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{C_RESET}",
                  file=sys.stderr)
            print(f"{C_CYAN}  FE UI 一次性激活链接 (浏览器打开 → 自动保存 token → 跳 /):{C_RESET}",
                  file=sys.stderr)
            print(f"{C_CYAN}  {link}{C_RESET}", file=sys.stderr)
            print(f"{C_CYAN}  (仅本次显示; 失去重发: pre_token.py rotate --label gui-default){C_RESET}",
                  file=sys.stderr)
            print(f"{C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{C_RESET}",
                  file=sys.stderr)

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
