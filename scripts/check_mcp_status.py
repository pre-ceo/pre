#!/usr/bin/env python3
"""scripts/check_mcp_status.py — 诊 sibling claude code 的 MCP 健康度.

检 5 项:
  1. ~/.local/bin/pre-mcp shim 是否有 PRE_CALLER_CWD plumbing (8e8475c+)
  2. ~/.pre/env::PRE_MCP_SECRET 绑 node prefix 还是严格 agent_id
  3. 每个 sibling 的 pre/agent_config.json 有 driver_type + mcp.caller_agent_id
  4. 每个 sibling 的 tmux session 是否在 (claude code 跑没跑)
  5. 每个 sibling 的 pre-mcp 子进程 env 是否含 PRE_CALLER_CWD 指向自己 cwd
     (= 该 session 在 shim 修复后重启过, MCP 调用会带正确 caller)

最后给一行建议: 哪些 sibling 需要 `tmux kill + pre spawn` 重启, 哪些已 OK.

read-only, 不修任何 db / env / token. 跑: python3 scripts/check_mcp_status.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRE_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PRE_ROOT, "src"))

from master.auth import hash_token       # noqa: E402
from master.persistence import MasterDB  # noqa: E402

CURSOR = Path.home() / "cursor"
ENV_PATH = Path.home() / ".pre" / "env"
DB_PATH = Path.home() / ".pre" / "data" / "master.db"
SHIM_PATH = Path.home() / ".local" / "bin" / "pre-mcp"

C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"


def _sym(ok: bool) -> str:
    return f"{C_GREEN}✓{C_RESET}" if ok else f"{C_RED}✗{C_RESET}"


def _warn() -> str:
    return f"{C_YELLOW}!{C_RESET}"


# ---------- global ----------

def check_shim() -> tuple[str, str]:
    if not SHIM_PATH.exists():
        return "MISSING", "~/.local/bin/pre-mcp 不存在 (跑 scripts/install.sh)"
    content = SHIM_PATH.read_text(encoding="utf-8")
    if "PRE_CALLER_CWD" in content:
        return "OK", "PRE_CALLER_CWD plumbing 已植入"
    return "STALE", "shim 缺 PRE_CALLER_CWD — pull pre + scripts/install.sh"


def _extract_env_secret() -> str | None:
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("PRE_MCP_SECRET="):
            v = s.split("=", 1)[1].strip()
            v = v.split("#", 1)[0].strip()
            v = v.strip('"').strip("'")
            return v or None
    return None


def check_env_token() -> tuple[str, str]:
    if not ENV_PATH.exists():
        return "NO_ENV", "~/.pre/env 不存在"
    secret = _extract_env_secret()
    if not secret:
        return "NO_SECRET", "PRE_MCP_SECRET 不在 ~/.pre/env"
    if not DB_PATH.exists():
        return "NO_DB", "~/.pre/data/master.db 不存在 (起一次 pre bus start)"
    db = MasterDB(str(DB_PATH))
    row = db.get_bus_token_by_hash(hash_token(secret))
    if not row:
        return "NOT_IN_DB", "PRE_MCP_SECRET hash 在 db 找不到"
    if row.get("revoked_ts"):
        return "REVOKED", f"label={row.get('label')} 已 revoke"
    aid = row.get("agent_id") or ""
    label = row.get("label") or "?"
    if not aid:
        return "NO_BINDING", f"label={label} 无 agent_id binding (老 db)"
    if "." in aid:
        return "STRICT", f"label={label}, binding={aid} → 跑 pre update 或 swap_mcp_secret_to_default.py"
    return "OK", f"label={label}, binding={aid} (node prefix)"


# ---------- per-sibling ----------

def list_siblings() -> list[Path]:
    if not CURSOR.exists():
        return []
    out = []
    for cfg_path in sorted(CURSOR.glob("*/pre/agent_config.json")):
        cwd = cfg_path.parent.parent
        # 排除非 sibling (pre_log 这种没 cli/driver 字段, 不当 sibling)
        try:
            d = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict) or not d.get("cli"):
            continue
        out.append(cwd)
    return out


def check_sibling_config(cwd: Path) -> tuple[bool, dict]:
    cfg_path = cwd / "pre" / "agent_config.json"
    try:
        d = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, {"error": "read/parse failed"}
    cli = d.get("cli")
    driver_type = d.get("driver_type")
    mcp_block = d.get("mcp") or {}
    caller_aid = mcp_block.get("caller_agent_id")
    ok = bool(cli and driver_type and caller_aid)
    return ok, {
        "cli": cli or "<missing>",
        "driver_type": driver_type or "<missing>",
        "caller_agent_id": caller_aid or "<missing>",
    }


def check_tmux_session(cwd: Path) -> tuple[bool, str]:
    name = cwd.name
    r = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"],
        capture_output=True, text=True,
    )
    return (r.returncode == 0), name


def _ps_env(pid: str) -> str:
    """macOS: `ps eww -p <pid>` 列 env 紧跟 command. Linux: 同款.
    解析 env 块比较脆弱, 这里仅返整 stdout 让 caller substring 查."""
    r = subprocess.run(
        ["ps", "eww", "-p", pid],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else ""


def find_pre_mcp_for_cwd(cwd: Path) -> list[tuple[str, bool]]:
    """找所有 pre-mcp 子进程, 返 [(pid, env_has_correct_PRE_CALLER_CWD)] for those
    whose env mentions this cwd."""
    r = subprocess.run(
        ["pgrep", "-f", "python.*-m pre_mcp"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    pids = [p for p in r.stdout.strip().split("\n") if p]
    matches = []
    cwd_s = str(cwd)
    for pid in pids:
        env = _ps_env(pid)
        if not env:
            continue
        # 该 pid env 是否含 PRE_CALLER_CWD=<this cwd>
        has_correct = f"PRE_CALLER_CWD={cwd_s}" in env
        # mention this cwd (PWD / OLDPWD / etc.) 但无 PRE_CALLER_CWD → 老 shim 跑的
        has_cwd_mention = cwd_s in env and not has_correct
        if has_correct or has_cwd_mention:
            matches.append((pid, has_correct))
    return matches


# ---------- main ----------

def main() -> int:
    print(f"{C_MAGENTA}━━━ check_mcp_status ━━━{C_RESET}")
    print()

    # global
    print(f"{C_BLUE}[global]{C_RESET}")
    shim_status, shim_detail = check_shim()
    print(f"  {_sym(shim_status == 'OK')} shim       {shim_status:8s}  {C_DIM}{shim_detail}{C_RESET}")
    env_status, env_detail = check_env_token()
    print(f"  {_sym(env_status == 'OK')} env token  {env_status:8s}  {C_DIM}{env_detail}{C_RESET}")
    print()

    # siblings
    print(f"{C_BLUE}[siblings]{C_RESET}")
    siblings = list_siblings()
    if not siblings:
        print(f"  {C_DIM}没在 ~/cursor/*/pre/agent_config.json 找到任何 sibling.{C_RESET}")
        return 0

    needs_restart = []
    for cwd in siblings:
        name = cwd.name
        cfg_ok, cfg_info = check_sibling_config(cwd)
        tmux_ok, tmux_name = check_tmux_session(cwd)
        mcp_matches = find_pre_mcp_for_cwd(cwd) if tmux_ok else []
        has_post_fix = any(correct for _pid, correct in mcp_matches)
        has_stale = any(not correct for _pid, correct in mcp_matches)

        # 综合判定
        if not cfg_ok:
            verdict = f"{C_YELLOW}NEEDS REPAIR{C_RESET}"
            hint = f"agent_config 缺字段 ({cfg_info}); 跑 `pre repair {cwd}`"
        elif not tmux_ok:
            verdict = f"{C_DIM}OFFLINE{C_RESET}"
            hint = f"tmux session 没起 (用到时 `pre spawn {cfg_info.get('caller_agent_id')}`)"
        elif has_post_fix:
            verdict = f"{C_GREEN}OK{C_RESET}"
            hint = "MCP 子进程含 PRE_CALLER_CWD, caller 解析正确"
        elif has_stale:
            verdict = f"{C_YELLOW}NEEDS RESTART{C_RESET}"
            hint = (f"pre-mcp 子进程跑老 shim 起的 (无 PRE_CALLER_CWD env). "
                    f"`pre spawn restart {name}` (短名 = tmux session 名)")
            needs_restart.append((tmux_name, name))
        else:
            verdict = f"{C_DIM}NO_MCP{C_RESET}"
            hint = "tmux 在, 但没找到 pre-mcp 子进程 (claude code 没起 / agent 不调 MCP)"

        print(f"  {C_CYAN}{name}{C_RESET}")
        print(f"    cwd:           {cwd}")
        print(f"    agent_config:  cli={cfg_info.get('cli')}, "
              f"driver_type={cfg_info.get('driver_type')}, "
              f"caller={cfg_info.get('caller_agent_id')}")
        print(f"    tmux session:  {'on' if tmux_ok else 'off'}{f' ({tmux_name})' if tmux_ok else ''}")
        print(f"    pre-mcp child: {len(mcp_matches)} 进程 "
              f"({'post-fix' if has_post_fix else 'pre-fix' if has_stale else 'none'})")
        print(f"    {verdict}  {C_DIM}{hint}{C_RESET}")
        print()

    # 总结建议
    print(f"{C_BLUE}[summary]{C_RESET}")
    if needs_restart:
        print(f"  {_warn()} {len(needs_restart)} sibling 需重启拿新 shim:")
        for _tmux_name, short in needs_restart:
            print(f"      pre spawn restart {short}")
    else:
        print(f"  {_sym(True)} all online siblings 都 post-fix, 无需重启")
    if shim_status != "OK" or env_status != "OK":
        print(f"  {_warn()} global 项不全, 先解决: shim={shim_status}, env_token={env_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
