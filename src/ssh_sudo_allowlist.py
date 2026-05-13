"""
pre/src/ssh_sudo_allowlist.py — SSH+Sudo 非写入命令 allowlist 层
import urllib.request
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

, , (agent-security 主 manager 持有定义).

决策链 (黑优于白):
  Layer 1 PCRE 预扫禁字 (>>?, <<?, $(...), backtick, ;, &&, ||, 末尾 &)
  Layer 2 shlex split 每段独立校验:
    a. dangerous_cmds (B5) → deny
    b. blacklist_override (B1 凭证 / B2 LLM token / B3 /proc / B4 deny_subcommands) → deny
    c. allow_prefixes (8 大类) → allow
    d. 不命中 → governor (fail-safe, M5/HC-PRE-2 严禁直接 allow)
  pipe 单层 OK + 右侧 not in pipe_right_deny

stdlib only (re + shlex + json + os + time, HC-PRE-1).

audit log pre_log/hook/ssh_sudo_audit.jsonl (chmod 600 + 按天 rotation).

config hot reload via mtime watch (M8 + 0-LLM-cost ms 级 IO 例外).
"""
from __future__ import annotations
import json
import os
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# token: lazy resolve from ~/.pre/env via token_resolver (PR3)
try:
    from src.common.token_resolver import resolve as _resolve_token  # hook context
except ImportError:
    from common.token_resolver import resolve as _resolve_token  # master context


# ---------- 路径 ----------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULE_ROOT = (_PROJECT_ROOT.parent / "pre_rule").resolve()
LOG_ROOT = (_PROJECT_ROOT.parent / "pre_log").resolve()
CONFIG_PATH = RULE_ROOT / "hook" / "ssh_sudo_allowlist.json"
AUDIT_DIR = LOG_ROOT / "hook"


# ---------- decision constants ----------
ALLOW = "allow"
DENY = "deny"
GOVERNOR = "governor"


# ---------- config cache (hot reload via mtime) ----------
_CONFIG_CACHE: dict | None = None
_CONFIG_MTIME: float | None = None


def _load_config() -> dict | None:
    """读 ssh_sudo_allowlist.json. mtime 变化时 reload (M8 hot reload).
    fail-safe: 不存在/解析失败返 None (调用方走 governor 降级)."""
    global _CONFIG_CACHE, _CONFIG_MTIME
    try:
        st = os.stat(CONFIG_PATH)
    except OSError:
        return None
    mtime = st.st_mtime
    if _CONFIG_CACHE is not None and _CONFIG_MTIME == mtime:
        return _CONFIG_CACHE
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    _CONFIG_CACHE = doc
    _CONFIG_MTIME = mtime
    return doc


# ---------- compiled regex cache ----------
_DENY_TOKENS_RE: re.Pattern | None = None
_CRED_GLOB_RE: re.Pattern | None = None
_LLM_GLOB_RE: re.Pattern | None = None
_PROC_RE: re.Pattern | None = None


def _compile_regexes(cfg: dict):
    """从 config 编译 regex (cache, mtime 变化重编)."""
    global _DENY_TOKENS_RE, _CRED_GLOB_RE, _LLM_GLOB_RE, _PROC_RE
    bl = cfg.get("blacklist_override", {})
    _DENY_TOKENS_RE = re.compile(cfg.get("deny_tokens_pcre", ""))
    cred_pat = bl.get("credential_glob_pcre", "")
    _CRED_GLOB_RE = re.compile(cred_pat) if cred_pat else None
    llm_pat = bl.get("llm_token_glob_pcre", "")
    _LLM_GLOB_RE = re.compile(llm_pat) if llm_pat else None
    proc_paths = bl.get("proc_sensitive") or []
    _PROC_RE = re.compile("|".join(proc_paths)) if proc_paths else None


def _ensure_compiled(cfg: dict):
    """惰性 compile, mtime 变化重编."""
    global _DENY_TOKENS_RE
    # 简单条件: regex 是 None 或 mtime 变了 → 重编
    # mtime 变化 _CONFIG_MTIME 已变, 上层 _load_config 命中再编
    if _DENY_TOKENS_RE is None:
        _compile_regexes(cfg)


# ---------- 匹配辅助 ----------

def _is_ssh_sudo(cmd: str) -> bool:
    """是否 ssh / sudo 类命令 (检查是否需要走本 allowlist 层)."""
    s = cmd.lstrip()
    return s.startswith("ssh ") or s.startswith("sudo ") or s.startswith("ssh\t") or s.startswith("sudo\t")


def _strip_ssh_wrapper(cmd: str) -> tuple[str, Optional[str]]:
    """ssh host 'inner_cmd' → (inner_cmd, host); 否则 (cmd, None).
    简单解析: 第一 token=ssh, 第二 token=host (跳过 -opts), 后续单引号/双引号包内容是 inner.
    返 (本机 cmd 或 inner cmd, host or None)."""
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return cmd, None
    if not tokens or tokens[0] != "ssh":
        return cmd, None
    # 跳过 ssh + 选项 + host
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        # 选项可能带 arg, e.g. -o key=val 是单 token (shlex 已合并)
        i += 1
    if i >= len(tokens):
        return cmd, None
    host = tokens[i]
    inner_tokens = tokens[i + 1:]
    if not inner_tokens:
        return "", host
    # inner_tokens 重新拼成 shell 串 (走 sudo allowlist 检查)
    inner_cmd = " ".join(inner_tokens)
    return inner_cmd, host


def _match_allow_prefix(cmd: str, allow_prefixes: dict) -> Optional[str]:
    """命中任一 8 大类 prefix → 返 类名 (e.g. 'log_read'). 不命中返 None."""
    for category, prefixes in allow_prefixes.items():
        for p in prefixes:
            if cmd.startswith(p):
                return category
    return None


def _match_blacklist(cmd: str, cfg: dict) -> Optional[tuple[str, str]]:
    """检查 blacklist_override. 返 (类别, 命中规则) 或 None."""
    bl = cfg.get("blacklist_override") or {}
    # B1 credential paths
    for path in (bl.get("credential_paths") or []):
        if path in cmd:
            return ("credential_path", path)
    # B1 credential glob
    if _CRED_GLOB_RE and _CRED_GLOB_RE.search(cmd):
        return ("credential_glob", _CRED_GLOB_RE.pattern)
    # B2 LLM token paths
    for path in (bl.get("llm_token_paths") or []):
        if path in cmd:
            return ("llm_token_path", path)
    if _LLM_GLOB_RE and _LLM_GLOB_RE.search(cmd):
        return ("llm_token_glob", _LLM_GLOB_RE.pattern)
    # B3 /proc 敏感
    if _PROC_RE and _PROC_RE.search(cmd):
        return ("proc_sensitive", _PROC_RE.pattern)
    # B5 dangerous_cmds (以 token 开头匹配)
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()
    if tokens:
        # 跳过 sudo 前缀
        i = 0
        while i < len(tokens) and tokens[i] in ("sudo", "ssh"):
            i += 1
        if i < len(tokens):
            first = tokens[i]
            # dangerous_cmds 含 "sh -c" 这种 multi-word, 手动检查
            for danger in (bl.get("dangerous_cmds") or []):
                if " " in danger:
                    parts = danger.split()
                    if tokens[i:i + len(parts)] == parts:
                        return ("dangerous_cmd", danger)
                else:
                    if first == danger:
                        return ("dangerous_cmd", danger)
    # B4 deny_subcommands (e.g. systemctl restart, pm2 stop)
    deny_subs = bl.get("deny_subcommands") or {}
    if tokens:
        i = 0
        while i < len(tokens) and tokens[i] in ("sudo", "ssh"):
            i += 1
        if i + 1 < len(tokens):
            cmd_name = tokens[i]
            sub = tokens[i + 1]
            if cmd_name in deny_subs and sub in deny_subs[cmd_name]:
                return ("deny_subcommand", f"{cmd_name} {sub}")
    return None


# ---------- 主入口: check ----------

def check(cmd: str, agent_id: str = "?") -> tuple[str, str, Optional[str]]:
    """
    主决策入口. 返 (decision, reason, matched_rule).
    decision ∈ {allow, deny, governor}.
    fail-safe: 任何异常 → governor (HC-PRE-2 严禁直接 allow).
    """
    started = time.time()
    cfg = _load_config()
    if not cfg:
        return GOVERNOR, "config_missing_or_bad", None
    _ensure_compiled(cfg)

    if not _is_ssh_sudo(cmd):
        # 不是 ssh/sudo, 不归本层管 (返 governor 让上层走其他规则)
        return GOVERNOR, "not_ssh_sudo", None

    # ---------- Layer 1 PCRE 预扫禁字 ----------
    # 单 pipe 是允许的 (右侧需在 pipe_right_deny 之外), 其他 token 命中即 deny
    # 简单做: 先检查 禁字, 但单 pipe 例外
    # deny_tokens_pcre 已含 (?<!\|)\|(?!\|) 这种"单 pipe 进 sub-rule" 的 token
    # 命中含义: 命令含 redirect/subshell/multi-cmd, 必拒
    # 但单 pipe 命中后, 我们不直接 deny, 而是走 sub-rule 路径
    has_pipe = bool(re.search(r"(?<!\|)\|(?!\|)", cmd))
    danger_tokens_no_pipe = re.compile(
        r"(?<!\\)(>>?|<<?|<<<|>&|&>|2>&1|\$\(|`|<\(|>\(|;|&&|\|\||&\s*$)"
    )
    if danger_tokens_no_pipe.search(cmd):
        return DENY, "deny_tokens_pcre", "deny_tokens"

    # ---------- pipe 单层处理 ----------
    if has_pipe:
        segments = [s.strip() for s in cmd.split("|")]
        if len(segments) > 5:
            return DENY, "too_many_pipes", "pipe_too_deep"
        # 第一段可以是 ssh/sudo, 后续段不能在 pipe_right_deny
        pipe_right_deny = cfg.get("pipe_right_deny") or []
        for seg in segments[1:]:
            seg_first = seg.split()[0] if seg else ""
            for danger in pipe_right_deny:
                if " " in danger:
                    parts = danger.split()
                    if seg.startswith(" ".join(parts)) or seg.split()[:len(parts)] == parts:
                        return DENY, f"pipe_right_deny: {danger}", "pipe_right_deny"
                else:
                    if seg_first == danger:
                        return DENY, f"pipe_right_deny: {danger}", "pipe_right_deny"
        # 主决策走第一段 (ssh/sudo 段)
        cmd_for_check = segments[0]
    else:
        cmd_for_check = cmd

    # ---------- ssh 包装拆解 ----------
    inner_cmd, ssh_host = _strip_ssh_wrapper(cmd_for_check)
    if ssh_host and inner_cmd:
        # ssh host 'sudo X' 模式: 检查 inner sudo cmd
        check_target = inner_cmd
    else:
        check_target = cmd_for_check

    # ---------- Layer 2 黑名单优先 ----------
    bl_match = _match_blacklist(check_target, cfg)
    if bl_match:
        return DENY, f"blacklist_override: {bl_match[0]} ({bl_match[1]})", f"blacklist:{bl_match[0]}"

    # ---------- Layer 2 白名单 prefix ----------
    allow_prefixes = cfg.get("allow_prefixes") or {}
    matched_cat = _match_allow_prefix(check_target.lstrip(), allow_prefixes)
    if matched_cat:
        return ALLOW, f"allowlist:{matched_cat}", f"allowlist:{matched_cat}"

    # ---------- 不命中 → governor (fail-safe, M5/HC-PRE-2) ----------
    return GOVERNOR, "no_allowlist_match", None


# ---------- audit log ----------

def write_audit(entry: dict):
    """pre_log/hook/ssh_sudo_audit_YYYYMMDD.jsonl, chmod 600 file + chmod 700 dir."""
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(AUDIT_DIR), 0o700)
        except OSError:
            pass
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = AUDIT_DIR / f"ssh_sudo_audit_{date_str}.jsonl"
        new_file = not log_file.exists()
        # M1 spec A: audit jsonl 全集 redact
        try:
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            from master.redact import safe_audit_dump as _safe_dump
            _line = _safe_dump(entry)
        except ImportError:
            _line = json.dumps(entry, ensure_ascii=False)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(_line + "\n")
        if new_file:
            try:
                os.chmod(str(log_file), 0o600)
            except OSError:
                pass
    except OSError:
        pass


def rotate_old_audit(days_keep: int = 30):
    """删 ssh_sudo_audit_*.jsonl mtime > 30 天."""
    if not AUDIT_DIR.exists():
        return
    cutoff = time.time() - days_keep * 86400
    for f in AUDIT_DIR.glob("ssh_sudo_audit_*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


# ---------- 高频 deny 限频告警 (M7, 联动) ----------

_DENY_WINDOW: dict[str, list[float]] = {}  # agent_id → [ts]
_DENY_WINDOW_SEC = 60.0
_DENY_THRESHOLD = 10
_LAST_ALERT_TS: dict[str, float] = {}  # agent_id → 最后告警 ts (防 spam)


def _check_deny_burst(agent_id: str) -> bool:
    """同 agent 60s 内 ≥10 deny → 触发 alert (返 True). 60s window 内只告警 1 次."""
    if not agent_id or agent_id == "?":
        return False
    now = time.time()
    arr = _DENY_WINDOW.setdefault(agent_id, [])
    arr[:] = [t for t in arr if t > now - _DENY_WINDOW_SEC]
    arr.append(now)
    if len(arr) < _DENY_THRESHOLD:
        return False
    # 已经告警过最近 60s 不再告警
    last = _LAST_ALERT_TS.get(agent_id, 0)
    if now - last < _DENY_WINDOW_SEC:
        return False
    _LAST_ALERT_TS[agent_id] = now
    return True


def _post_alert_user_default(agent_id: str, latest_cmd: str):
    """POST /api/v1/agents/user.default/send kind=alert severity=warning."""
    import urllib.request
    import urllib.error
    master_url = os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500")
    token = _resolve_token("hook")
    body = {
        "from_agent": "pre.allowlist",
        "from_role": "platform",
        "kind": "alert",
        "payload": {
            "text": f"[security] {agent_id} 60s 内 ≥{_DENY_THRESHOLD} 次 ssh+sudo deny — 可能 prompt injection brute force. 最后命令: {latest_cmd[:120]}",
            "priority": "high",  # warning 级别用 high (critical 是 burst cap 100/min 留 P0)
            "severity": "warning",
            "alert_type": "ssh_sudo_deny_burst",
            "agent_id": agent_id,
            "deny_count_60s": _DENY_THRESHOLD,
        },
    }
    try:
        req = urllib.request.Request(
            master_url.rstrip("/") + "/api/v1/agents/user.default/send",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with _NO_PROXY_OPENER.open(req, timeout=5) as r:
            r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        pass


# ---------- 高级入口: check + audit + 限频 ----------

def check_with_audit(cmd: str, agent_id: str = "?", host: str = "") -> tuple[str, str, Optional[str]]:
    """主入口. check + audit + 高频 deny 告警."""
    started = time.time()
    decision, reason, matched_rule = check(cmd, agent_id)
    latency_ms = int((time.time() - started) * 1000)

    write_audit({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
        "cmd": cmd[:500],
        "decision": decision,
        "matched_rule": matched_rule or "",
        "denial_reason": reason if decision == DENY else "",
        "host": host or "",
        "latency_ms": latency_ms,
    })

    # 高频 deny 告警 (M7)
    if decision == DENY and _check_deny_burst(agent_id):
        _post_alert_user_default(agent_id, cmd)

    return decision, reason, matched_rule
