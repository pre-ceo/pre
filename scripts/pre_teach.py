#!/usr/bin/env python3
"""pre teach — 发一份 pre 用法教程给 agent. 幂等, 跟 pre 仓库实时同步.

用法:
  pre teach me                # cwd/pre/agent_config.json 推 caller, 给自己
  pre teach <agent_id>        # 给指定 agent
  pre teach me --no-bus       # 不走 bus, 只写文件 + stdout
  pre teach me --no-stdout    # 只走 bus + 文件, 不 stdout
  pre teach me --no-file      # 不写 cwd/pre/teach.md, 只 stdout/bus

3 个落点:
  1. cwd/pre/teach.md         — 文件 persistent (任何 driver 都能 read), 幂等覆盖
  2. bus send_message         — kind=teach, payload.text=教程, ping agent 去 read
  3. stdout                   — 直接打印 (claude `!pre teach me` 看到)

教程内容按 caller 的 cli (claude/codex/gemini) customize:
  - memory 机制不同 (claude 走 ~/.claude/projects memory, codex/gemini 没原生 → pre/teach.md re-read)
  - hook 不同 (claude 有 PreToolUse/Stop, codex/gemini 走 driver 内嵌 evaluator)
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRE_ROOT = os.path.dirname(_HERE)

C_CYAN = "\033[36m"
C_YELLOW = "\033[33m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"


_VALID_CLIS = ("claude", "codex", "gemini")

# memory 机制按 driver 分流. claude 是 auto memory; codex/gemini 没原生 memory,
# 靠 cwd/pre/teach.md 文件层 + 重启 re-read.
_DRIVER_MEMORY_HINT = {
    "claude": (
        "你支持 auto memory 系统. 读完本教程后, 把要点存到:\n"
        "  ~/.claude/projects/<project-dir-encoded>/memory/\n"
        "  - 单条 memory 写成 <type>_<slug>.md (frontmatter type=user/feedback/project/reference)\n"
        "  - 加 entry 到 memory/MEMORY.md (always-loaded index)\n"
        "细则见 ~/.claude/CLAUDE.md 的 'auto memory' 章节. 教程 idempotent — 已记的不要重复写, "
        "新增/更新即可."
    ),
    "codex": (
        "codex CLI 没原生 persistent memory. 关键要点存:\n"
        "  - cwd/pre/notes.md (你自己加的 agent-local notes)\n"
        "  - 或 cwd/pre/agent_config.json 的 metadata 字段\n"
        "本教程已写到 cwd/pre/teach.md, 重启时 re-read 即可拿到最新版."
    ),
    "gemini": (
        "gemini CLI 没原生 persistent memory. 同 codex: 关键要点存 cwd/pre/notes.md "
        "或 agent_config.json metadata. 本教程已写到 cwd/pre/teach.md, 重启 re-read 拿最新版."
    ),
}

_DRIVER_HOOK_HINT = {
    "claude": (
        "你的 PreToolUse + Stop hook 在 .claude/settings.json (cmd=pre-tool-use / pre-stop-hook).\n"
        "PreToolUse: 工具调用前 governor 评估 (allow/ask/deny).\n"
        "Stop: agent 输出结束后, 写 finding / 通知 / report."
    ),
    "codex": (
        "你没有 .claude/settings.json hook. approval 评估走 codex driver 内嵌 evaluator, "
        "在 src/drivers/cli_codex_local/driver.py."
    ),
    "gemini": (
        "你没有 .claude/settings.json hook. approval 评估走 gemini driver 内嵌 evaluator, "
        "在 src/drivers/cli_gemini_local/driver.py."
    ),
}

_DRIVER_SHELL_ESCAPE_HINT = {
    "claude": (
        "你能用 `!cmd` shell escape — `!` 之后的命令在 user shell 跑, stdout 进你的 context. "
        "例如 `!pre teach me` 触发本教程更新."
    ),
    "codex": "codex 没有 `!` shell escape, 但可以用 Bash tool 等价跑 shell.",
    "gemini": "gemini 没有 `!` shell escape, 但可以用 shell tool 等价.",
}


def _git_short_sha() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", _PRE_ROOT, "rev-parse", "--short=8", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return "unknown"


def _read_file(path: str, max_chars: int = 0) -> str:
    if not os.path.isfile(path):
        return f"[file not present: {path}]\n"
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[...truncated at {max_chars} chars, full: {path}]\n"
        return text
    except OSError as e:
        return f"[read failed: {path}: {e}]\n"


def _build_teach_text(cli: str, target_agent_id: str, target_cwd: str) -> str:
    """拼教程 markdown. cli 决定 driver-specific 段落."""
    sha = _git_short_sha()
    cli = cli if cli in _VALID_CLIS else "claude"
    parts = [
        f"# pre — agent 入门教程\n\n",
        f"_自动生成: pre={sha}, cli={cli}, target={target_agent_id}, cwd={target_cwd}_\n\n",
        f"由 `pre teach` 实时从 pre 仓拼出. 教程更新时重跑 `pre teach me` 拿最新版.\n\n",

        f"## 1. pre 项目定位 + agent 接入路径\n\n",
        f"以下来自 {_PRE_ROOT}/CLAUDE.md (节选):\n\n",
        f"```markdown\n",
        _read_file(os.path.join(_PRE_ROOT, "CLAUDE.md"), max_chars=6000),
        f"\n```\n\n",

        f"## 2. 你 ({cli}) 的特定接入\n\n",
        f"### shell escape\n{_DRIVER_SHELL_ESCAPE_HINT[cli]}\n\n",
        f"### hook / approval\n{_DRIVER_HOOK_HINT[cli]}\n\n",

        f"## 3. MCP 工具 (mcp__pre__*)\n\n",
        f"agent ↔ master 通信主路径. 你能调:\n\n",
        f"- `mcp__pre__send_message(to_agent, kind, payload, parent_id?)` — 派单给其他 agent\n",
        f"- `mcp__pre__fetch_inbox(since?, limit?, kind?)` — 取自己 inbox\n",
        f"- `mcp__pre__read_pane(agent_id, ...)` — 看 agent tmux pane (跨 node 严限)\n",
        f"- `mcp__pre__cycle_state(agent_id?)` — 看 cycle 状态\n\n",
        f"严禁直接 curl master HTTP — token 会进 transcript + 失去 from_agent binding.\n\n",

        f"## 4. 形成记忆 (按你的 driver)\n\n",
        f"{_DRIVER_MEMORY_HINT[cli]}\n\n",

        f"## 5. 标准 workflow\n\n",
        f"- 改动后: 跑 `pre status` 验 subsystem + cron + wiring 全绿\n",
        f"- commit message: 英文 + 中文双语 body (本仓 commit 约定)\n",
        f"- audit: 派给 opensource agent (`mcp__pre__send_message` to_agent=local.cli-claude-code-local.opensource kind=command), 等 PASS reply 再 push\n",
        f"- 跨 sibling repo (pre / pre_ui / pre_rule / opensource) 改动: 走 bus 派给对方 agent, 不直接改文件\n\n",

        f"## 6. findings 机制\n\n",
        f"- 写 finding 文件到 cwd/pre/findings/{{LEVEL}}-{{title}}.md (LEVEL=CRITICAL/WARNING/INFO)\n",
        f"- Stop hook 自动 report + notify\n",
        f"- 处理完移到 cwd/pre/findings/processed/\n\n",

        f"## 7. 下一步\n\n",
        f"读完本教程, 按上面 §4 形成 memory. 之后再跑 `pre teach me` 拿教程 diff (pre 文档变了内容跟着变).\n\n",

        f"---\n",
        f"教程文件路径 (file-system layer, 任何 driver 都能 re-read): {target_cwd}/pre/teach.md\n",
    ]
    return "".join(parts)


def _caller_from_cwd(cwd: str) -> tuple[str, str]:
    """从 cwd/pre/agent_config.json 推 (agent_id, cli). 缺则返 ('', '')."""
    cfg = os.path.join(cwd, "pre", "agent_config.json")
    if not os.path.isfile(cfg):
        return "", ""
    try:
        with open(cfg, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "", ""
    if not isinstance(d, dict):
        return "", ""
    cli = d.get("cli") or ""
    mcp = d.get("mcp") if isinstance(d.get("mcp"), dict) else {}
    aid = mcp.get("caller_agent_id") or ""
    if not aid:
        node = os.environ.get("PRE_NODE_ID", "local")
        dt = d.get("driver_type") or ""
        pn = d.get("project_name") or ""
        if dt and pn:
            aid = f"{node}.{dt}.{pn}"
    return aid, cli


def _agent_cwd_from_pointer(agent_id: str) -> str:
    """从 pre_rule/agents/<dir>/agent_pointer.json 反查 cwd. 返 '' 找不到."""
    try:
        from config import RULE_ROOT  # type: ignore
        rule_root = os.environ.get("PRE_RULE_ROOT") or RULE_ROOT
    except ImportError:
        rule_root = os.environ.get("PRE_RULE_ROOT", "")
    agents_dir = os.path.join(rule_root, "agents") if rule_root else ""
    if not agents_dir or not os.path.isdir(agents_dir):
        return ""
    for name in os.listdir(agents_dir):
        ptr = os.path.join(agents_dir, name, "agent_pointer.json")
        if not os.path.isfile(ptr):
            continue
        try:
            with open(ptr, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("agent_id") == agent_id:
                return d.get("cwd") or ""
        except (OSError, json.JSONDecodeError):
            continue
    return ""


def _load_pre_env():
    """读 ~/.pre/env 注入 environ."""
    p = os.path.join(os.path.expanduser("~"), ".pre", "env")
    if not os.path.isfile(p):
        return
    try:
        for line in open(p, encoding="utf-8").read().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            v = v.strip().strip('"').strip("'")
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


def _send_via_master(to_agent: str, text: str, teach_file: str) -> tuple[bool, str]:
    """走 master POST /api/v1/agents/{to_agent}/send. 用 PRE_HOOK_SECRET (loopback)."""
    _load_pre_env()
    tok = os.environ.get("PRE_HOOK_SECRET")
    if not tok:
        return False, "PRE_HOOK_SECRET not in ~/.pre/env (run scripts/pre_token.py issue --role hook)"
    master_url = os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500")
    body = json.dumps({
        # master kind whitelist: chat / command / cron_trigger / ...
        # 教程是 informational → 用 chat. subject 字段标 'pre teach' 让 agent 在
        # fetch_inbox 时可识别.
        "kind": "chat",
        "payload": {
            "subject": "pre teach — 教程更新",
            "teach_file": teach_file,
            "text": text,
            "hint": (
                f"教程已写到 {teach_file} (文件 persistent + idempotent). "
                f"请 Read 该文件 + 按教程 §4 form memory. 教程会跟 pre 仓库一起变, "
                f"下次再跑 `pre teach me` 拿 diff."
            ),
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{master_url}/api/v1/agents/{to_agent}/send",
        data=body,
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.load(r)
        return True, d.get("msg_id", "?")
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    p = argparse.ArgumentParser(
        prog="pre teach",
        description="发一份 pre 用法教程给 agent (idempotent, 跟 pre 仓库实时同步).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("target", nargs="?", default="me",
                   help="'me' (默认, cwd/pre/agent_config 推) 或 explicit agent_id")
    p.add_argument("--cli", choices=list(_VALID_CLIS), default=None,
                   help="强制 cli flavor (默认从 agent_config.cli 读)")
    p.add_argument("--no-bus", action="store_true",
                   help="不走 bus, 只写文件 + stdout")
    p.add_argument("--no-stdout", action="store_true",
                   help="不 print stdout (但仍写文件 + 走 bus)")
    p.add_argument("--no-file", action="store_true",
                   help="不写 cwd/pre/teach.md (但仍走 bus + stdout)")
    args = p.parse_args()

    cwd = os.getcwd()
    target = args.target

    if target == "me":
        target_aid, target_cli = _caller_from_cwd(cwd)
        target_cwd = cwd
        if not target_aid:
            print(f"{C_YELLOW}[warn]{C_RESET} 推不出 caller agent_id (cwd/pre/agent_config.json 缺/坏). "
                  f"建议 `pre init <cwd> --driver claude|codex|gemini` 先初始化, "
                  f"或显式 `pre teach <agent_id>`.", file=sys.stderr)
            target_aid = ""
            target_cli = args.cli or "claude"  # fallback
    else:
        target_aid = target
        target_cwd = _agent_cwd_from_pointer(target_aid) or cwd
        # 从 pointer cwd 再读 cli
        _, target_cli = _caller_from_cwd(target_cwd)
        if not target_cli:
            target_cli = args.cli or "claude"

    if args.cli:
        target_cli = args.cli

    teach_text = _build_teach_text(target_cli, target_aid or "(unbound)", target_cwd)

    teach_file = os.path.join(target_cwd, "pre", "teach.md")
    if not args.no_file:
        try:
            os.makedirs(os.path.dirname(teach_file), exist_ok=True)
            with open(teach_file, "w", encoding="utf-8") as f:
                f.write(teach_text)
            print(f"{C_CYAN}[file]{C_RESET}   wrote {teach_file} ({len(teach_text)} chars)",
                  file=sys.stderr)
        except OSError as e:
            print(f"{C_YELLOW}[warn]{C_RESET} write {teach_file} failed: {e}", file=sys.stderr)

    if not args.no_bus and target_aid:
        ok, info = _send_via_master(target_aid, teach_text, teach_file)
        if ok:
            print(f"{C_CYAN}[bus]{C_RESET}    sent to {target_aid} (msg_id={info})",
                  file=sys.stderr)
        else:
            print(f"{C_YELLOW}[bus]{C_RESET}    send failed: {info}", file=sys.stderr)
    elif not args.no_bus:
        print(f"{C_DIM}[bus skip]{C_RESET} 无 caller agent_id, 跳 bus", file=sys.stderr)

    if not args.no_stdout:
        print(teach_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
