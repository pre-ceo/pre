"""
freerun_intervention_loop — finding-driven 补丁记录 + Tiered Review + cry-wolf 防御.

用途:
  freerun mode 下 PreToolUse / Stop hook 触发 ASK→deny 时, 调用
  record_intervention() 写 patches.jsonl + alert user.default.
  Tier 分级 + cry-wolf 防 prompt injection 后疯狂触发自补丁.

复用 (ssh_sudo_allowlist 模式) + 007 (notify_abstract / user.default).

API:
  record_intervention(agent_id, cmd, deny_reason, tier) -> dict
  check_cry_wolf(agent_id) -> (in_cooldown, until_ts)
  list_pending_patches(since=0, limit=50) -> list
  rotate_old_audit(days_keep=30) -> int

 引入.
HC-PRE-1 stdlib only + HC-PRE-2 fail-safe + cry-wolf.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from common.paths import PRE_LOG_ROOT
from typing import Optional

# token: lazy resolve from ~/.pre/env via token_resolver (PR3)
try:
    from src.common.token_resolver import resolve as _resolve_token  # hook context
except ImportError:
    from common.token_resolver import resolve as _resolve_token  # master context

# Loopback master call: direct, bypass proxy env (Surge etc.)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 路径常量
_PRE_LOG = Path(os.environ.get(
    "PRE_LOG_DIR",
    PRE_LOG_ROOT,
))
_PATCHES_DIR = _PRE_LOG / "rules"
_ARCHIVE_DIR = _PATCHES_DIR / "archive"
_CRY_WOLF_PATH = Path(os.environ.get(
    "PRE_CRY_WOLF_STATE",
    str(_PRE_LOG / "rules" / "cry_wolf_state.json"),
))

# cry-wolf threshold ()
_CRY_WOLF_WINDOW_SEC = 86400      # 24h 内
_CRY_WOLF_THRESHOLD = 5            # ≥5 次 T2/T3
_CRY_WOLF_COOLDOWN_SEC = 21600    # 6h cooldown

# Tier severity 映射 (M6 / )
_TIER_TO_SEVERITY = {
    "T1": None,           # 不通知 (自动 merge)
    "T2": "warning",
    "T3": "critical",
    "T4": None,           # 仅 audit, 不通知 (HC-U-i 永拒)
}

# Prompt injection 净化 pattern ( / M5)
_FORBIDDEN_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # ESC \x1b 等
_BASE64_LONG = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")          # 长 base64
_SHELL_METACHAR_BURST = re.compile(r"[\$\{\}\[\]<>|&;`]{5,}")    # shell metachar 集中


def _sanitize_input(text: str, field_name: str = "?") -> tuple[str, list[str]]:
    """净化输入. 返 (sanitized, suspicious_patterns).
    suspicious_patterns 非空 → 调用方应升 Tier 3 (M5 / )."""
    if not isinstance(text, str):
        return str(text)[:500], ["non_string_input"]
    issues = []
    # 控制字符 → 拒
    if _FORBIDDEN_CTRL.search(text):
        issues.append("forbidden_ctrl_char")
    # 长 base64 → 怀疑
    if _BASE64_LONG.search(text):
        issues.append("long_base64")
    # shell metachar burst → 怀疑
    if _SHELL_METACHAR_BURST.search(text):
        issues.append("shell_metachar_burst")
    # 截断 + 替换控制字符
    sanitized = _FORBIDDEN_CTRL.sub("", text)[:2000]
    return sanitized, issues


def _cmd_hash(cmd: str) -> str:
    """命令指纹 (sha256 前 12 字符), 用于 patch_id / finding 文件名."""
    return hashlib.sha256(cmd.encode("utf-8")).hexdigest()[:12]


def _load_cry_wolf_state() -> dict:
    """{agent_id: {events: [ts...], cooldown_until: ts}}. fail-safe → 空 dict."""
    try:
        if not _CRY_WOLF_PATH.exists():
            return {}
        with open(_CRY_WOLF_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cry_wolf_state(state: dict):
    """fail-safe 保存."""
    try:
        _CRY_WOLF_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_CRY_WOLF_PATH.parent), 0o700)
        except OSError:
            pass
        with open(_CRY_WOLF_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        try:
            os.chmod(str(_CRY_WOLF_PATH), 0o600)
        except OSError:
            pass
    except OSError:
        pass


def check_cry_wolf(agent_id: str) -> tuple[bool, float]:
    """MH-12: 同 agent 24h ≥5 次 T2/T3 → 6h cooldown.
    返 (in_cooldown, cooldown_until_ts). in_cooldown=True 时上层应跳过 alert / 仅 audit."""
    state = _load_cry_wolf_state()
    a = state.get(agent_id) or {}
    now = time.time()
    until = float(a.get("cooldown_until") or 0)
    return (now < until, until)


def _record_cry_wolf_event(agent_id: str, tier: str) -> tuple[bool, float]:
    """记一次 T2/T3 事件. 返 (just_triggered_cooldown, cooldown_until)."""
    if tier not in ("T2", "T3"):
        return False, 0.0
    state = _load_cry_wolf_state()
    a = state.setdefault(agent_id, {"events": [], "cooldown_until": 0})
    now = time.time()
    # 清理 24h 外的旧事件
    a["events"] = [ts for ts in a["events"] if now - ts < _CRY_WOLF_WINDOW_SEC]
    a["events"].append(now)
    triggered = False
    if len(a["events"]) >= _CRY_WOLF_THRESHOLD and now >= a.get("cooldown_until", 0):
        a["cooldown_until"] = now + _CRY_WOLF_COOLDOWN_SEC
        triggered = True
    state[agent_id] = a
    _save_cry_wolf_state(state)
    return triggered, a["cooldown_until"]


def _patches_log_path() -> Path:
    """pre_log/rules/patches_YYYYMMDD.jsonl."""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _PATCHES_DIR / f"patches_{date_str}.jsonl"


def _write_patch_audit(entry: dict):
    """patches_YYYYMMDD.jsonl chmod 600 按天 rotation. fail-safe."""
    try:
        _PATCHES_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_PATCHES_DIR), 0o700)
        except OSError:
            pass
        log_file = _patches_log_path()
        new_file = not log_file.exists()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if new_file:
            try:
                os.chmod(str(log_file), 0o600)
            except OSError:
                pass
    except OSError:
        pass


def _alert_user_default(agent_id: str, cmd: str, tier: str, deny_reason: str):
    """走 master HTTP /agents/user.default/send kind=alert (MH-10).
    severity: T2 warning / T3 critical / T4 不通知 / T1 不通知."""
    severity = _TIER_TO_SEVERITY.get(tier)
    if not severity:
        return  # T1 / T4 不通知, 仅 audit
    master_url = os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500").rstrip("/")
    token = _resolve_token("hook")
    body = {
        "kind": "alert",
        "from_agent": agent_id,
        "from_role": "freerun-worker",
        "payload": {
            "text": f"[freerun intervention {tier}] {agent_id}\n\ncmd: {cmd[:200]}\nreason: {deny_reason}",
            "severity": severity,
            "tier": tier,
            "deny_reason": deny_reason,
        },
    }
    req = urllib.request.Request(
        f"{master_url}/api/v1/agents/user.default/send",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        _NO_PROXY_OPENER.open(req, timeout=5).read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        pass  # fail-safe


def record_intervention(agent_id: str, cmd: str, deny_reason: str,
                         tier: str, source: str = "PreToolUse") -> dict:
    """主入口: 记一次 freerun intervention.

    流程:
      1. 净化 cmd (M5 prompt injection)
      2. 检查 cry-wolf cooldown ()
      3. 写 patches.jsonl audit log (M4)
      4. 不在 cooldown 且 tier ∈ {T2, T3} → alert user.default (M6)
      5. 记 cry-wolf 事件

    返 {patch_id, tier, in_cry_wolf, cooldown_until, alert_sent, suspicious}.
    """
    # 1. 输入净化 + 升 tier ()
    sanitized_cmd, suspicious = _sanitize_input(cmd, "cmd")
    sanitized_reason, _ = _sanitize_input(deny_reason, "reason")
    if suspicious and tier in ("T1", "T2"):
        # 异常 pattern 自动升 Tier 3 (M5)
        tier = "T3"

    # 2. cry-wolf check
    in_cooldown, cooldown_until = check_cry_wolf(agent_id)

    # 3. patches.jsonl audit (即使 cooldown 也 audit)
    patch_id = _cmd_hash(sanitized_cmd)
    ts_iso = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts": ts_iso,
        "patch_id": patch_id,
        "source": source,
        "agent_id": agent_id,
        "cmd": sanitized_cmd,
        "deny_reason": sanitized_reason,
        "tier": tier,
        "suspicious": suspicious,
        "in_cry_wolf": in_cooldown,
        "decision": "audit_only" if in_cooldown else "alert_pending_review",
    }
    _write_patch_audit(entry)

    # 4. alert (除非 cry-wolf cooldown 中)
    alert_sent = False
    if not in_cooldown and tier in ("T2", "T3"):
        _alert_user_default(agent_id, sanitized_cmd, tier, sanitized_reason)
        alert_sent = True

    # 5. 记 cry-wolf 事件 (即使在 cooldown 中也累计)
    triggered, until = _record_cry_wolf_event(agent_id, tier)
    if triggered and not in_cooldown:
        # 刚刚触发了新 cooldown, 也通知一下 (M9 cry-wolf trigger)
        _alert_user_default(
            agent_id,
            f"[cry-wolf triggered] agent {agent_id} 24h ≥{_CRY_WOLF_THRESHOLD} 次 T2/T3 介入",
            "T3",
            "cry_wolf_threshold_reached",
        )
        cooldown_until = until

    return {
        "patch_id": patch_id,
        "tier": tier,
        "in_cry_wolf": in_cooldown,
        "cooldown_until": cooldown_until,
        "alert_sent": alert_sent,
        "suspicious": suspicious,
    }


def list_pending_patches(since: float = 0, limit: int = 50) -> list[dict]:
    """读最近 N 天的 patches_*.jsonl, 返 decision=alert_pending_review 的 entry."""
    if not _PATCHES_DIR.exists():
        return []
    out = []
    files = sorted(_PATCHES_DIR.glob("patches_*.jsonl"), reverse=True)
    for f in files[:30]:  # 最多看 30 天
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        ts_epoch = datetime.fromisoformat(
                            e.get("ts", "").replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        continue
                    if ts_epoch < since:
                        continue
                    if e.get("decision") == "alert_pending_review":
                        out.append(e)
                    if len(out) >= limit:
                        return out
        except OSError:
            continue
    return out


def rotate_old_audit(days_keep: int = 30) -> int:
    """归档 days_keep 天前的 patches_*.jsonl 到 archive/<YYYY-MM>/. 返归档文件数."""
    if not _PATCHES_DIR.exists():
        return 0
    now = time.time()
    cutoff = now - days_keep * 86400
    moved = 0
    for f in _PATCHES_DIR.glob("patches_*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                date_part = f.stem.replace("patches_", "")  # YYYYMMDD
                if len(date_part) == 8:
                    yymm = f"{date_part[:4]}-{date_part[4:6]}"
                    archive_subdir = _ARCHIVE_DIR / yymm
                    archive_subdir.mkdir(parents=True, exist_ok=True)
                    target = archive_subdir / f.name
                    f.rename(target)
                    moved += 1
        except OSError:
            continue
    return moved
