#!/usr/bin/env python3
"""
pre-update — git pull pre + pre_ui, optional uv sync + bus restart.

用法:
  pre-update [--no-ui] [--no-sync] [--no-restart]

行为 (任一步失败即停, pre repo 由 $PRE_ROOT 自动检测):
  1. pre repo working tree 必须 clean (dirty 拒绝, 不 stash)
  2. git pull --ff-only (非 ff 拒绝, 不 rebase)
  3. 同样处理 pre_ui (默认 sibling, $PRE_UI_PATH override; --no-ui skip)
  4. uv sync 在 pre repo (--no-sync skip; 新版可能加依赖)
  5. pre bus restart (--no-restart skip; daemon 重起拿新代码)

不动 pre_rule (那是 user personal config; install.sh 才会强更 system.md).
"""
import argparse
import os
import subprocess
import sys

C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRE_ROOT = os.path.dirname(_HERE)


def _git(repo: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True, text=True, check=check,
    )


def _head_short(repo: str) -> str:
    r = _git(repo, "rev-parse", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else "?"


def _pull_repo(repo: str, name: str) -> int:
    print(f"\n{C_MAGENTA}━━━ {name} @ {repo} ━━━{C_RESET}")
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"{C_YELLOW}not a git repo, skipping{C_RESET}")
        return 0

    dirty = _git(repo, "status", "--porcelain").stdout.strip()
    if dirty:
        print(f"{C_YELLOW}working tree dirty, refusing pull:{C_RESET}")
        print(C_DIM + dirty + C_RESET)
        print(f"{C_YELLOW}commit / stash / discard first, then re-run pre update{C_RESET}")
        return 1

    before = _head_short(repo)
    print(f"{C_BLUE}before:{C_RESET} {before}")

    r = _git(repo, "pull", "--ff-only")
    if r.returncode != 0:
        print(f"{C_YELLOW}pull failed (likely non-ff or network):{C_RESET}")
        print((r.stderr or r.stdout).strip())
        return 1

    after = _head_short(repo)
    if before == after:
        print(f"{C_DIM}already up-to-date{C_RESET}")
    else:
        print(f"{C_CYAN}after:{C_RESET}  {after} (was {before})")
    return 0


def _uv_sync(pre_repo: str) -> int:
    print(f"\n{C_MAGENTA}━━━ uv sync @ {pre_repo} ━━━{C_RESET}")
    try:
        r = subprocess.run(["uv", "sync"], cwd=pre_repo)
    except FileNotFoundError:
        print(f"{C_YELLOW}uv not found in PATH; skip with --no-sync or install uv{C_RESET}")
        return 1
    if r.returncode != 0:
        print(f"{C_YELLOW}uv sync failed (rc={r.returncode}){C_RESET}")
        print(f"{C_DIM}tip: UV_HTTP_TIMEOUT=300 uv sync, 或 source ~/rule.sh 让 proxy 生效{C_RESET}")
    return r.returncode


def _bus_restart() -> int:
    print(f"\n{C_MAGENTA}━━━ pre bus restart ━━━{C_RESET}")
    bus_sh = os.path.join(_HERE, "bus_ctl.sh")
    return subprocess.call(["bash", bus_sh, "restart"])


def _ensure_mcp_env_binding() -> int:
    """Idempotent guard: 若 ~/.pre/env::PRE_MCP_SECRET 绑死单个 agent_id
    (含 '.', e.g. local.cli-claude-code-local.pre), auto-rotate 到 node-prefix
    mcp-default token (binding='local'). 已正确则 no-op.

    引入背景: 8e8475c pre-mcp shim 加 PRE_CALLER_CWD 后, sibling repos 的
    MCP 调用真实 caller_agent_id 不再被误识别成 'pre'; 但若 env 用的是严格
    绑定 token, master verify_token 反而拒掉 sibling. 老机器 `pre update` 走
    一次这里就自动迁好.

    失败不阻 update — 仅 warn."""
    print(f"\n{C_MAGENTA}━━━ ensure MCP env binding (node prefix) ━━━{C_RESET}")
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    try:
        from _token_lib import ensure_mcp_env_uses_node_prefix
    except ImportError as e:
        print(f"{C_YELLOW}[skip] _token_lib import failed: {e}{C_RESET}")
        return 0
    try:
        result = ensure_mcp_env_uses_node_prefix()
    except Exception as e:  # noqa: BLE001 — guard step, 不阻 update
        print(f"{C_YELLOW}[skip] unexpected error: {type(e).__name__}: {e}{C_RESET}")
        return 0

    status = result.get("status")
    if status == "ok":
        reason = result.get("reason", "?")
        # already_node_prefix 是 happy path; 其他 ok 是 benign edge cases
        if reason == "already_node_prefix":
            print(f"{C_DIM}[ok] PRE_MCP_SECRET 已绑 node prefix "
                  f"(label='{result.get('label')}'), no-op.{C_RESET}")
        else:
            print(f"{C_DIM}[ok] {reason} — no-op{C_RESET}")
        return 0

    if status == "swapped":
        print(f"{C_CYAN}[swapped]{C_RESET} {result.get('old_label')} "
              f"({C_DIM}bound={result.get('old_bound_agent_id')}{C_RESET}) "
              f"→ {result.get('new_label')} "
              f"({C_DIM}bound={result.get('new_bound_agent_id')}{C_RESET})")
        print(f"{C_DIM}     ~/.pre/env PRE_MCP_SECRET {result.get('env_marker')} (mode 600); "
              f"raw 未打印{C_RESET}")
        print(f"{C_DIM}     sibling claude code session 用到时需重启读新 env "
              f"(tmux kill + pre spawn){C_RESET}")
        return 0

    # error
    reason = result.get("reason", "?")
    print(f"{C_YELLOW}[warn] {reason}{C_RESET}")
    if "hint" in result:
        print(f"{C_DIM}     {result['hint']}{C_RESET}")
    if reason == "env_rewrite_failed":
        # 应急 raw 必须给 user 让其手动写回
        print(f"{C_YELLOW}     db 已 rotated 但 env 写失败. 手动写到 ~/.pre/env:{C_RESET}")
        print(f"       PRE_MCP_SECRET={result.get('raw_emergency')}")
    return 0  # 失败不阻 update


def _refresh_mcp() -> int:
    """重 register pre mcp shim 给 claude/codex/gemini. mv repo / 升级后必跑,
    确保各 cli 的 mcp config command 指当前 PRE_ROOT 的 ~/.local/bin/pre-mcp shim.
    cli 没装 → skip 不 fail. mcp 子进程 long-lived, agent 需重启才生效."""
    print(f"\n{C_MAGENTA}━━━ refresh mcp registration ━━━{C_RESET}")
    shim = os.path.expanduser("~/.local/bin/pre-mcp")
    if not os.path.isfile(shim):
        print(f"{C_YELLOW}shim {shim} 不存在 — 先跑 scripts/install.sh{C_RESET}")
        return 1
    # claude: 走 install_mcp_registration.py (~/.claude.json diff+overwrite)
    reg_py = os.path.join(_HERE, "install_mcp_registration.py")
    if os.path.isfile(reg_py):
        rc = subprocess.call(["python3", reg_py, "--pre-root", _PRE_ROOT])
        if rc == 0:
            print(f"{C_CYAN}[ok]{C_RESET}    claude  -> {shim}")
        else:
            print(f"{C_YELLOW}[warn]{C_RESET} claude register rc={rc}")
    # codex / gemini: 走各自 cli mcp 子命令
    import shutil
    for cli in ("codex", "gemini"):
        if not shutil.which(cli):
            print(f"{C_DIM}[skip]{C_RESET}  {cli} not installed")
            continue
        # gemini cli 默认 scope=project 会写到 cwd .gemini/settings.json, 必须显式 user
        if cli == "gemini":
            rm_args = [cli, "mcp", "remove", "--scope", "user", "pre"]
            add_args = [cli, "mcp", "add", "--scope", "user", "pre", shim]
        else:
            rm_args = [cli, "mcp", "remove", "pre"]
            add_args = [cli, "mcp", "add", "pre", shim]
        subprocess.run(rm_args, capture_output=True, text=True)  # 老 entry 删, 不 fail
        r = subprocess.run(add_args, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"{C_CYAN}[ok]{C_RESET}    {cli}    -> {shim}")
        else:
            print(f"{C_YELLOW}[warn]{C_RESET} {cli} register failed: "
                  f"{(r.stderr or r.stdout).strip()[:200]}")
    print(f"{C_DIM}注: mcp 子进程 long-lived, agent 需 /quit + exec 重启才生效{C_RESET}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="pre update",
        description="git pull pre + pre_ui, optional uv sync + bus restart.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--no-ui", action="store_true",
                   help="skip pre_ui pull")
    p.add_argument("--no-sync", action="store_true",
                   help="skip uv sync in pre")
    p.add_argument("--no-restart", action="store_true",
                   help="skip pre bus restart (daemon keeps running old code)")
    p.add_argument("--no-mcp-refresh", action="store_true",
                   help="skip refresh mcp registration in claude/codex/gemini")
    p.add_argument("--no-mcp-env-check", action="store_true",
                   help="skip ~/.pre/env PRE_MCP_SECRET binding auto-rotate guard")
    args = p.parse_args()

    pre_repo = _PRE_ROOT
    pre_ui_repo = os.environ.get("PRE_UI_PATH") or os.path.join(
        os.path.dirname(pre_repo), "pre_ui"
    )

    rc = _pull_repo(pre_repo, "pre")
    if rc != 0:
        return rc

    if not args.no_ui:
        if os.path.isdir(pre_ui_repo):
            rc = _pull_repo(pre_ui_repo, "pre_ui")
            if rc != 0:
                print(f"{C_YELLOW}pre_ui pull failed; pre 已 updated. 手动处理后再 retry{C_RESET}")
                return rc
        else:
            print(f"\n{C_DIM}pre_ui dir not found at {pre_ui_repo}, skipping{C_RESET}")

    if not args.no_sync:
        if _uv_sync(pre_repo) != 0:
            return 1

    if not args.no_mcp_refresh:
        _refresh_mcp()  # 失败不阻 bus restart, 仅打 warn

    # idempotent guard: 8e8475c shim 修后, 老机器 env 可能仍指严格绑定 token,
    # 走一次自动 rotate 到 node prefix. happy path 是 no-op.
    if not args.no_mcp_env_check:
        _ensure_mcp_env_binding()

    if not args.no_restart:
        if _bus_restart() != 0:
            return 1
    else:
        print(f"\n{C_YELLOW}skipping bus restart (--no-restart); daemon 还跑老代码{C_RESET}")
        print(f"{C_DIM}手动跑: pre bus restart{C_RESET}")

    print(f"\n{C_CYAN}━━━ update ok ━━━{C_RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
