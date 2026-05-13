#!/usr/bin/env python3
"""
启动 pre Master.

用法:
  uv run python scripts/start_master.py [--host 127.0.0.1] [--port 19500]

multi-token RBAC: 不再接受 --secret. token 全在 master.db 的 bus_tokens 表.
启动三步 (都 idempotent):
  1. bootstrap — 检查 6 个 default label (node/operator/cli/mcp/gui/hook),
     缺哪个就 issue, raw 追加 ~/.pre/data/initial_tokens.txt (chmod 600).
  2. env sync — 扫 initial_tokens.txt, 把 default label 的 raw 同步到 ~/.pre/env
     对应 PRE_<KIND>_SECRET (only if-key-not-set). 升级路径自动 surface PRE_GUI_SECRET.
  3. capability sync — 算 6 default 的 sha256[:12] 写到
     pre_rule/hook/read_pane_capability.json 的 allow (没文件就建). fresh 机器
     fe ui 调 sse-ticket / read_pane 不被默认 deny.

新颁发或新同步 gui-default 时, stderr 输出 fe ui 一次性激活 magic link
(token 走 URL fragment, 不进 server log).

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


def _bootstrap_tokens(db: MasterDB, db_path: str) -> list[tuple[str, str]]:
    """Idempotent bootstrap: 检查 6 个 default label 在不在 db, 缺哪个补哪个.

    返本次新颁发的 [(label, raw)] 列表 (空 = 全 6 都已存在).
    新 raw append 到 initial_tokens.txt; env sync 由 _sync_env_from_initial_tokens 统一处理.
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
                "# 启动还会 sync 到 ~/.pre/env 对应 PRE_<KIND>_SECRET (if-not-set).\n"
                "# Use scripts/pre_token.py issue ... 给特定 agent / 来源发更细粒度 token.\n\n"
            )
        for label, raw in issued:
            f.write(f"{label}={raw}\n")
    try:
        os.chmod(init_file, 0o600)
    except OSError:
        pass

    return issued


def _sync_env_from_initial_tokens(db_path: str) -> list[tuple[str, str]]:
    """读 initial_tokens.txt 全部 default label, 跟 ~/.pre/env 比, 缺的 PRE_<KIND>_SECRET 补.

    返本次 append 到 env 的 [(env_key, raw)] 列表.
    用途: 升级路径 — db 已 bootstrap 但 env 从未 sync 时, 自动补齐 + 重显 gui magic link.
    """
    init_file = Path(db_path).parent / "initial_tokens.txt"
    if not init_file.exists():
        return []

    env_file = Path.home() / ".pre" / "env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing_env = ""
    if env_file.exists():
        try:
            existing_env = env_file.read_text(encoding="utf-8")
        except OSError:
            existing_env = ""
    existing_lines = existing_env.split("\n")

    # parse initial_tokens.txt → {label: raw}
    label_to_raw: dict[str, str] = {}
    try:
        for line in init_file.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            label, _, raw = line.partition("=")
            label = label.strip()
            raw = raw.strip()
            if label and raw:
                label_to_raw[label] = raw
    except OSError:
        return []

    synced: list[tuple[str, str]] = []
    appends: list[str] = []
    for label, env_key in _LABEL_TO_ENV_KEY.items():
        if label not in label_to_raw:
            continue
        if any(line.lstrip().startswith(f"{env_key}=") for line in existing_lines):
            continue
        appends.append(f"{env_key}={label_to_raw[label]}  # synced from initial_tokens.txt ({label})")
        synced.append((env_key, label_to_raw[label]))

    if not appends:
        return []

    with open(env_file, "a", encoding="utf-8") as f:
        if existing_env and not existing_env.endswith("\n"):
            f.write("\n")
        f.write("# synced from initial_tokens.txt (bootstrap default labels)\n")
        f.write("\n".join(appends) + "\n")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    return synced


def _sync_capability_from_initial_tokens(db_path: str) -> list[str]:
    """Idempotent: 算 initial_tokens.txt 6 个 default label 的 sha256[:12],
    同步到 pre_rule/hook/read_pane_capability.json 的 allow 列表 (用户可手编加条目).

    fresh 机器文件不存在 → 创建; 已存在但缺某 default 的 caller hash → 补 (不删用户加的).
    返本次新加的 label 列表 (空 = 都已在).
    用途: fe ui (gui-default) / 跨 token caller 调 sse-ticket 等 read_pane endpoint 不被默认 deny.
    """
    import hashlib
    import json as _json

    init_file = Path(db_path).parent / "initial_tokens.txt"
    if not init_file.exists():
        return []

    rule_root = os.environ.get("PRE_RULE_ROOT")
    if not rule_root:
        return []
    cap_path = Path(rule_root) / "hook" / "read_pane_capability.json"
    cap_path.parent.mkdir(parents=True, exist_ok=True)

    # parse initial_tokens.txt → {label: raw}
    label_to_raw: dict[str, str] = {}
    try:
        for line in init_file.read_text(encoding="utf-8").split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            label, _, raw = line.partition("=")
            label_to_raw[label.strip()] = raw.strip()
    except OSError:
        return []

    # 6 default → reason map (其他 label e.g. rotate 后的 gui-default.<ts> 不在这里, 用户自己 capability)
    label_reasons = {
        "node-default":     "node-default token (node daemon)",
        "operator-default": "operator-default token (admin / browser GUI ops)",
        "cli-default":      "cli-default token (legacy one-shot CLI)",
        "mcp-default":      "mcp-default token (pre_mcp child process)",
        "gui-default":      "gui-default token (pre_ui browser)",
        "hook-default":     "hook-default token (hook/runtime modules)",
    }

    # 算 hash (跟 master server.py _check_auth 一致: sha256("Bearer <raw>")[:12])
    label_hashes: list[tuple[str, str]] = []
    for label in label_reasons:
        raw = label_to_raw.get(label)
        if not raw:
            continue
        h = hashlib.sha256(f"Bearer {raw}".encode("utf-8")).hexdigest()[:12]
        label_hashes.append((label, h))

    if not label_hashes:
        return []

    # load existing capability.json (or fresh)
    existing: dict
    if cap_path.exists():
        try:
            existing = _json.loads(cap_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, _json.JSONDecodeError):
            existing = {}
    else:
        existing = {}

    existing.setdefault("version", 1)
    existing.setdefault("default", "deny")
    existing.setdefault("allow", [])
    existing.setdefault("deny", [])

    existing_callers = {
        entry.get("caller")
        for entry in existing["allow"]
        if isinstance(entry, dict) and entry.get("caller")
    }

    added: list[str] = []
    for label, hash_ in label_hashes:
        if hash_ in existing_callers:
            continue
        existing["allow"].append({
            "caller": hash_,
            "target": "local.*",
            "reason": label_reasons[label],
        })
        added.append(label)

    if not added:
        return []

    cap_path.write_text(_json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    try:
        os.chmod(cap_path, 0o600)
    except OSError:
        pass
    return added


def _print_magic_link(gui_raw: str):
    link = _magic_link(gui_raw)
    print(file=sys.stderr)
    print(f"{C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{C_RESET}",
          file=sys.stderr)
    print(f"{C_CYAN}  FE UI 一次性激活链接 (浏览器打开 → 自动保存 token → 跳 /):{C_RESET}",
          file=sys.stderr)
    print(f"{C_CYAN}  {link}{C_RESET}", file=sys.stderr)
    print(f"{C_CYAN}  (失去重发: pre_token.py rotate --label gui-default){C_RESET}",
          file=sys.stderr)
    print(f"{C_CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{C_RESET}",
          file=sys.stderr)


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
    db.conn.close()  # 让 run_master 自己重新打开 (避免共享 connection)

    # env sync (扫 initial_tokens.txt 补 env 缺的 PRE_<KIND>_SECRET)
    synced = _sync_env_from_initial_tokens(args.db)

    # capability sync (写 pre_rule/hook/read_pane_capability.json 让 default token 通过 ACL)
    synced_cap = _sync_capability_from_initial_tokens(args.db)

    if issued:
        init_file = str(Path(args.db).parent / "initial_tokens.txt")
        print(f"{C_MAGENTA}[master] ━━━ bootstrap: {len(issued)} new default token(s) issued ━━━{C_RESET}",
              file=sys.stderr)
        print(f"{C_MAGENTA}[master] appended to {init_file} (chmod 600){C_RESET}",
              file=sys.stderr)
        for label, raw in issued:
            print(f"{C_BLUE}[master]   {label:18s}  {raw}{C_RESET}", file=sys.stderr)

    if synced:
        env_file = str(Path.home() / ".pre" / "env")
        print(f"{C_MAGENTA}[master] ━━━ env sync: {len(synced)} PRE_<KIND>_SECRET appended to {env_file} ━━━{C_RESET}",
              file=sys.stderr)
        for env_key, _raw in synced:
            print(f"{C_BLUE}[master]   {env_key}{C_RESET}", file=sys.stderr)

    if synced_cap:
        rule_root = os.environ.get("PRE_RULE_ROOT", "<PRE_RULE_ROOT>")
        cap_file = str(Path(rule_root) / "hook" / "read_pane_capability.json")
        print(f"{C_MAGENTA}[master] ━━━ capability sync: {len(synced_cap)} default token(s) allow'd in {cap_file} ━━━{C_RESET}",
              file=sys.stderr)
        for label in synced_cap:
            print(f"{C_BLUE}[master]   {label}{C_RESET}", file=sys.stderr)

    # magic link: 优先 issued gui (新颁发), 否则 synced gui (db 已有但 env 刚补)
    gui_raw = next((raw for lb, raw in issued if lb == "gui-default"), None)
    if not gui_raw:
        gui_raw = next((raw for k, raw in synced if k == "PRE_GUI_SECRET"), None)
    if gui_raw:
        _print_magic_link(gui_raw)

    if issued or synced or synced_cap:
        print(f"{C_CYAN}[master] 后续 token 管理用 `python3 scripts/pre_token.py issue/list/revoke/rotate`{C_RESET}",
              file=sys.stderr)

    # secret 参数已废弃, 传空字符串 (server.py 里 _check_auth 不读)
    try:
        asyncio.run(run_master(args.host, args.port, args.db, secret=""))
    except KeyboardInterrupt:
        print("\n[master] stopped.")


if __name__ == "__main__":
    main()
