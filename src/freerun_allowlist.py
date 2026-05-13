"""
freerun_allowlist — freerun mode 下的命令白名单 + tier 分级.

用途:
  freerun-worker agent (agent-research 等) 触发 PreToolUse hook 时, 检查命令是否
  命中 freerun_allowlist (T1 自动 ALLOW), 命中 blacklist 升 tier (T3/T4).

复用 ssh_sudo_allowlist 模式 (allow_prefixes / blacklist_override
/ deny_tokens_pcre / pipe_right_deny). 跟 ssh_sudo_allowlist 平行 (不替代).

API:
  check(cmd, agent_id) -> (decision, reason, tier)
    decision ∈ {"allow", "deny", "ask"}
    tier ∈ {"T1", "T2", "T3", "T4"}

 引入.
HC-PRE-1 (stdlib only) + HC-PRE-2 (fail-safe → ask) + (fail-closed).
"""
from __future__ import annotations
import json
import os
import re
import time
from pathlib import Path
from common.paths import PRE_RULE_ROOT
from typing import Optional

# 路径常量
_RULE_PATH = Path(os.environ.get(
    "PRE_FREERUN_ALLOWLIST",
    str(Path(PRE_RULE_ROOT) / "freerun" / "freerun_allowlist.json"),
))
_KILL_SWITCH_FILE = Path(os.environ.get(
    "PRE_FREERUN_KILL_SWITCH_FILE",
    str(Path(PRE_RULE_ROOT) / "freerun" / "kill_switch.flag"),
))

# 决策值
ALLOW = "allow"
DENY = "deny"
ASK = "ask"

# 内部 cache
_CACHE: dict = {"mtime": 0.0, "cfg": None, "compiled": None}


def is_kill_switch_active() -> bool:
    """MH-13: env var FREERUN_KILL_SWITCH=1 或 file flag 存在 → True.
    触发时 freerun 转半 freerun (上层处理 deny → ask 逻辑, 本层只检测)."""
    if os.environ.get("FREERUN_KILL_SWITCH", "").strip() in ("1", "true", "yes"):
        return True
    try:
        return _KILL_SWITCH_FILE.exists()
    except OSError:
        return False


def _load_config() -> Optional[dict]:
    """mtime hot reload, fail-safe (HC-PRE-2): config 不可读 → 返 None, check 返 ask."""
    try:
        if not _RULE_PATH.exists():
            return None
        mtime = _RULE_PATH.stat().st_mtime
        if _CACHE["cfg"] is not None and _CACHE["mtime"] == mtime:
            return _CACHE["cfg"]
        with open(_RULE_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        _CACHE["cfg"] = cfg
        _CACHE["mtime"] = mtime
        _CACHE["compiled"] = None  # invalidate compiled regex
        return cfg
    except (OSError, json.JSONDecodeError):
        return None


def _ensure_compiled(cfg: dict):
    """编译 deny_tokens_pcre + 各 glob_pcre 一次, cache 在 _CACHE['compiled']."""
    if _CACHE.get("compiled") is not None:
        return _CACHE["compiled"]
    compiled = {
        "deny_tokens": None,
        "credential_glob": None,
        "llm_token_glob": None,
        "proc_sensitive": [],
    }
    try:
        if cfg.get("deny_tokens_pcre"):
            compiled["deny_tokens"] = re.compile(cfg["deny_tokens_pcre"])
    except re.error:
        pass
    bl = cfg.get("blacklist_override") or {}
    try:
        if bl.get("credential_glob_pcre"):
            compiled["credential_glob"] = re.compile(bl["credential_glob_pcre"], re.IGNORECASE)
    except re.error:
        pass
    try:
        if bl.get("llm_token_glob_pcre"):
            compiled["llm_token_glob"] = re.compile(bl["llm_token_glob_pcre"], re.IGNORECASE)
    except re.error:
        pass
    for pat in (bl.get("proc_sensitive") or []):
        try:
            compiled["proc_sensitive"].append(re.compile(pat))
        except re.error:
            pass
    _CACHE["compiled"] = compiled
    return compiled


def _match_blacklist(cmd: str, cfg: dict, compiled: dict) -> Optional[tuple[str, str, str]]:
    """返 (category, matched, tier) — 命中即升 tier (T3/T4)."""
    bl = cfg.get("blacklist_override") or {}
    tier_cfg = cfg.get("tier_classification") or {}

    # 1. credential paths (T4)
    for path in (bl.get("credential_paths") or []):
        if path in cmd:
            return ("credential_path", path, tier_cfg.get("blacklist_credential_path", "T4"))

    # 2. credential / llm token glob (T4 / T3)
    if compiled.get("credential_glob") and compiled["credential_glob"].search(cmd):
        return ("credential_glob", "pcre_match", tier_cfg.get("blacklist_credential_path", "T4"))
    if compiled.get("llm_token_glob") and compiled["llm_token_glob"].search(cmd):
        return ("llm_token_glob", "pcre_match", tier_cfg.get("blacklist_credential_path", "T4"))
    for path in (bl.get("llm_token_paths") or []):
        if path in cmd:
            return ("llm_token_path", path, tier_cfg.get("blacklist_credential_path", "T4"))

    # 3. proc sensitive (T4)
    for pat in compiled.get("proc_sensitive", []):
        if pat.search(cmd):
            return ("proc_sensitive", pat.pattern, tier_cfg.get("blacklist_proc_sensitive", "T4"))

    # 4. deny subcommands (T3)
    for sub in (bl.get("deny_subcommands") or []):
        if sub in cmd:
            return ("deny_subcommand", sub, tier_cfg.get("blacklist_deny_subcommand", "T3"))

    # 5. dangerous_cmds (T3)
    for danger in (bl.get("dangerous_cmds") or []):
        if cmd.startswith(danger) or f" {danger}" in cmd:
            return ("dangerous_cmd", danger, tier_cfg.get("blacklist_dangerous_cmd", "T3"))

    return None


def _match_allow_prefix(cmd: str, allow_prefixes: dict) -> Optional[str]:
    """返命中的 category 名 (e.g. git_read / fs_read), 否则 None."""
    for cat, prefixes in allow_prefixes.items():
        for p in (prefixes or []):
            if cmd.startswith(p):
                return cat
    return None


def _check_pipe_right(cmd: str, cfg: dict) -> Optional[tuple[str, str]]:
    """单 pipe 检查右侧是否在 deny list. 返 (matched, tier) 或 None."""
    if "|" not in cmd:
        return None
    segments = [s.strip() for s in cmd.split("|")]
    if len(segments) > 5:
        return ("pipe_too_deep", "T2")
    pipe_right_deny = cfg.get("pipe_right_deny") or []
    for seg in segments[1:]:
        seg_first = seg.split()[0] if seg else ""
        for danger in pipe_right_deny:
            if seg_first == danger:
                return (f"pipe_right_deny:{danger}", "T2")
    return None


def check(cmd: str, agent_id: str = "?") -> tuple[str, str, str]:
    """主决策. 返 (decision, reason, tier).
    decision ∈ {allow, deny, ask}. tier ∈ {T1, T2, T3, T4}.

    fail-safe ( + HC-PRE-2): 任何异常 → ask (上层 freerun mode 默 deny).
    kill switch active → 永远 ask (转半 freerun, ASK 路径).
    """
    if not cmd or not cmd.strip():
        return ASK, "empty_cmd", "T2"

    if is_kill_switch_active():
        return ASK, "kill_switch_active", "T2"

    cfg = _load_config()
    if not cfg:
        # fail-closed: config 不可读 → ask (上层 freerun 默 deny)
        return ASK, "config_missing_or_bad", "T2"

    try:
        compiled = _ensure_compiled(cfg)

        # Layer 1: deny_tokens_pcre (单 pipe 例外, 走下面 pipe_right_deny)
        # 不含单 pipe 的 redirect/subshell 命中即 T2 deny
        danger_no_pipe = re.compile(
            r"(?<!\\)(>>?|<<?|<<<|>&|&>|2>&1|\$\(|`|<\(|>\(|;|&&|\|\||&\s*$)"
        )
        if danger_no_pipe.search(cmd):
            return DENY, "deny_tokens_pcre", "T2"

        # Layer 2: pipe right deny
        pipe_check = _check_pipe_right(cmd, cfg)
        if pipe_check:
            return DENY, pipe_check[0], pipe_check[1]
        # 主决策段: 第一段
        cmd_for_check = cmd.split("|")[0].strip()

        # Layer 3: blacklist 优先
        bl = _match_blacklist(cmd_for_check, cfg, compiled)
        if bl:
            cat, matched, tier = bl
            return DENY, f"blacklist:{cat}({matched})", tier

        # Layer 4: allow_prefixes
        allow_prefixes = cfg.get("allow_prefixes") or {}
        matched_cat = _match_allow_prefix(cmd_for_check, allow_prefixes)
        if matched_cat:
            return ALLOW, f"allowlist:{matched_cat}", "T1"

        # 不命中: T2 ask (上层 freerun mode 默 deny)
        tier_cfg = cfg.get("tier_classification") or {}
        return ASK, "no_allowlist_match", tier_cfg.get("no_match_fall_through", "T2")

    except Exception as e:
        # fail-closed: 任何异常 → ask
        return ASK, f"exception:{type(e).__name__}", "T2"
