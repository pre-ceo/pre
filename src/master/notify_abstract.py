"""
pre/src/master/notify_abstract.py — 通知抽象层 ()

对 user.default virtual agent 的 chat/alert message 抽象成多 channel 触发:
- WebhookTTSChannel: HTTPS POST webhook-notify-post (TTS critical alert, group_id 来自 chmod 600 config)
- CliSendkeysChannel: tmux send-keys 给 user attached fn_* cli pane
- MasterLogChannel (兜底): print master stderr

设计:
- 每 channel send(text, priority, payload) → SendResult(ok, channel, error?)
- send_all() 按 priority 偏好顺序调多 channel, allSettled 风格 (任一失败不影响其他)
- HC-PRE-2 fail-safe: 全 try/except 包裹, 不抛
- HC-A7 凭证: webhook-notify group_id 仅从 pre_rule/notify_config.json chmod 600 读, 不进 git/audit/chat
- 子条款: stdlib only (urllib + subprocess + json), 0 第三方

audit 写: pre_log/cron/mobile_audit_YYYYMMDD.jsonl, chmod 600.
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------- 路径 ----------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RULE_ROOT = (_PROJECT_ROOT.parent / "pre_rule").resolve()
LOG_ROOT = (_PROJECT_ROOT.parent / "pre_log").resolve()
NOTIFY_CONFIG_PATH = RULE_ROOT / "notify_config.json"
MOBILE_AUDIT_DIR = LOG_ROOT / "cron"

# ---------- channel 偏好 per priority ----------
PRIORITY_CHANNEL_PREFS = {
    "critical": ["webhook-notify", "cli_sendkeys"],   # 双发
    "high":     ["webhook-notify", "cli_sendkeys"],   # webhook-notify 优先, cli fallback
    "normal":   ["cli_sendkeys", "master_log"],  # cli 优先, log 兜底
}


# ---------- result ----------

@dataclass
class SendResult:
    ok: bool
    channel: str
    error: str = ""


# ---------- audit ----------

def _ensure_audit_dir():
    MOBILE_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    # chmod 700 dir, 600 file (M5 安全)
    try:
        os.chmod(str(MOBILE_AUDIT_DIR), 0o700)
    except OSError:
        pass


def _write_audit(entry: dict):
    """append 一行到 pre_log/cron/mobile_audit_YYYYMMDD.jsonl, chmod 600.
    schema: {ts, from_agent, to_user, priority, channel, payload_size, status, error?}
    payload 全文不入 audit (M10)."""
    try:
        _ensure_audit_dir()
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = MOBILE_AUDIT_DIR / f"mobile_audit_{date_str}.jsonl"
        new_file = not log_file.exists()
        # M1 spec A: audit jsonl 全集 redact
        try:
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
    except OSError as e:
        print(f"[notify-abstract] audit write failed: {e}", flush=True)


def rotate_old_audit(days_keep: int = 30):
    """删 mobile_audit_*.jsonl mtime > 30 天 (轻量 pre 自管, 不上 logrotate)."""
    if not MOBILE_AUDIT_DIR.exists():
        return
    cutoff = time.time() - days_keep * 86400
    for f in MOBILE_AUDIT_DIR.glob("mobile_audit_*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


# ---------- channels ----------

class NotifyChannel:
    name = "abstract"

    async def send(self, text: str, priority: str, payload: dict,
                   agent_from: str = "") -> SendResult:
        raise NotImplementedError


class WebhookTTSChannel(NotifyChannel):
    name = "webhook-notify"

    def __init__(self):
        self._config = self._load_config()

    def _load_config(self) -> dict:
        try:
            with open(NOTIFY_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    async def send(self, text: str, priority: str, payload: dict,
                   agent_from: str = "") -> SendResult:
        cfg = self._config
        group_id = cfg.get("group_id") or cfg.get("webhook-notify_group_id")
        base_url = cfg.get("base_url") or cfg.get("webhook-notify_base_url") or "https://webhook-notify-post.lzq.dev"
        # v3 (user hint): webhook-notify 后端 IP 白名单. master 跑 Mac 本地 IP 不在,
        # 走 sshaio socks2http 代理 → 出口走 whitelisted 服务器 (aws-jp1c-nilsunsafex 同 batpm).
        # 默认 19201 (nilsunsafe socks2http), 可通过 notify_config.json::webhook-notify_proxy 覆盖.
        proxy_url = cfg.get("webhook-notify_proxy", "http://127.0.0.1:19201")
        if not group_id:
            return SendResult(ok=False, channel=self.name, error="no_group_id_in_config")

        url = base_url.rstrip("/") + "/api/v1/group/tts"
        # v4: webhook-notify API body schema: tts_text (not text), 实测 nilsunsafex 直发返
        # "tts_text or body is required". 修字段名 + body 也加备选
        body = {
            "group_id": group_id,
            "tts_text": text[:500],
            "body": text[:500],
            "priority": priority,
        }
        try:
            req_data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                url, data=req_data,
                headers={
                    "Content-Type": "application/json",
                    # v5: webhook-notify 走 Cloudflare, urllib 默认 UA "Python-urllib"
                    # 被 CF Browser Integrity Check (error 1010) 拒. 改 curl 类 UA
                    "User-Agent": "curl/8.0.0",
                },
                method="POST",
            )
            await asyncio.to_thread(_urlopen_check_via_proxy, req, proxy_url)
            return SendResult(ok=True, channel=self.name)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
            return SendResult(ok=False, channel=self.name, error=f"{type(e).__name__}: {str(e)[:200]}")


def _urlopen_check_via_proxy(req: urllib.request.Request, proxy_url: str):
    """同步 urlopen 走 HTTP proxy (sshaio socks2http), 跑在 thread 里. 8s timeout."""
    if proxy_url:
        # 用 ProxyHandler 走代理. https 流量也通过 http 代理 CONNECT.
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        })
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open(req, timeout=8) as r:
            r.read()
    else:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()


class CliSendkeysChannel(NotifyChannel):
    """v2 (user 反馈): 用 tmux display-message toast 替代 send-keys -l.
    - 不再污染 user input box (堆叠 + 没 Enter 不美观)
    - critical 用更长 display-time + 持久状态行 (status-left)
    - normal/high 用短 toast (3s)
    name 仍叫 cli_sendkeys 保持 audit 兼容性."""
    name = "cli_sendkeys"

    async def send(self, text: str, priority: str, payload: dict,
                   agent_from: str = "") -> SendResult:
        session = await asyncio.to_thread(self._find_attached_session)
        if not session:
            return SendResult(ok=False, channel=self.name, error="no_attached_session")

        # 提示文字 (≤200 字截断)
        prefix = f"[{agent_from or 'agent'} P={priority}]"
        msg = f"{prefix} {text[:200]}"

        # critical 显示更长, normal/high 短
        display_ms = 8000 if priority == "critical" else 4000

        try:
            await asyncio.to_thread(self._tmux_toast, session, msg, display_ms)
            return SendResult(ok=True, channel=self.name)
        except (subprocess.SubprocessError, OSError) as e:
            return SendResult(ok=False, channel=self.name,
                              error=f"{type(e).__name__}: {str(e)[:200]}")

    @staticmethod
    def _find_attached_session() -> Optional[str]:
        """tmux list-sessions, 找 attached=1 且 name 属于 user 常驻 cli."""
        try:
            r = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}"],
                capture_output=True, text=True, timeout=3,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if r.returncode != 0:
            return None
        attached = []
        for line in (r.stdout or "").splitlines():
            if ":1" in line:
                name = line.split(":")[0]
                attached.append(name)
        if not attached:
            return None
        for pref in ("pre", "agent-ceo", "fn_dispatcher"):
            if pref in attached:
                return pref
        for n in attached:
            if n.startswith("fn_"):
                return n
        return None

    @staticmethod
    def _tmux_toast(session: str, text: str, display_ms: int = 4000):
        """tmux display-message: 状态栏短暂 toast, 不污染 input box, 不堆栈."""
        # display-time 临时设, 显示完原值不动 (tmux 会自动清)
        subprocess.run(
            ["tmux", "display-message", "-t", session,
             "-d", str(display_ms), text],
            check=True, timeout=3, capture_output=True,
        )


class MasterLogChannel(NotifyChannel):
    name = "master_log"

    async def send(self, text: str, priority: str, payload: dict,
                   agent_from: str = "") -> SendResult:
        try:
            print(f"[user-notify P={priority}] from={agent_from} | {text[:300]}",
                  file=sys.stderr, flush=True)
            return SendResult(ok=True, channel=self.name)
        except Exception as e:
            return SendResult(ok=False, channel=self.name, error=str(e)[:200])


# ---------- 单例实例 (lazy init) ----------

_NILSAPN_CHANNEL: Optional[WebhookTTSChannel] = None
_CLI_CHANNEL: Optional[CliSendkeysChannel] = None
_LOG_CHANNEL: Optional[MasterLogChannel] = None


def _get_channel(name: str) -> Optional[NotifyChannel]:
    global _NILSAPN_CHANNEL, _CLI_CHANNEL, _LOG_CHANNEL
    if name == "webhook-notify":
        if _NILSAPN_CHANNEL is None:
            _NILSAPN_CHANNEL = WebhookTTSChannel()
        return _NILSAPN_CHANNEL
    if name == "cli_sendkeys":
        if _CLI_CHANNEL is None:
            _CLI_CHANNEL = CliSendkeysChannel()
        return _CLI_CHANNEL
    if name == "master_log":
        if _LOG_CHANNEL is None:
            _LOG_CHANNEL = MasterLogChannel()
        return _LOG_CHANNEL
    return None


# ---------- public API ----------

async def send_all(text: str, priority: str, payload: dict,
                   agent_from: str, to_user: str = "user.default") -> dict:
    """按 priority 偏好顺序调 channels, allSettled 风格.
    任一 channel 失败不阻塞其他, 全失败也不抛 (HC-PRE-2).
    返回 {ok: bool, channels: [SendResult], audited: bool}."""
    if priority not in PRIORITY_CHANNEL_PREFS:
        priority = "normal"
    pref = PRIORITY_CHANNEL_PREFS[priority]
    results = []
    for ch_name in pref:
        ch = _get_channel(ch_name)
        if not ch:
            continue
        try:
            result = await ch.send(text, priority, payload, agent_from=agent_from)
        except Exception as e:
            result = SendResult(ok=False, channel=ch_name, error=f"unexpected: {e!r}"[:200])
        results.append(result)

    any_ok = any(r.ok for r in results)
    # audit 一次 (覆盖所有 channels 尝试)
    payload_size = len(json.dumps(payload, ensure_ascii=False)) if payload else 0
    # text_preview 字段 (脱敏 ≤100 char) — agent-security 子条款 (b) M2.
    # 写时一次脱敏 SENSITIVE_PATTERNS, audit 文件本身就 safe; endpoint 读时直接返.
    try:
        from master.redact import redact_for_audit
        text_preview, matched_patterns = redact_for_audit(text, max_len=100)
    except ImportError:
        text_preview, matched_patterns = "", {}
    for r in results:
        _write_audit({
            "ts": datetime.now(timezone.utc).isoformat(),
            "from_agent": agent_from,
            "to_user": to_user,
            "priority": priority,
            "channel": r.channel,
            "payload_size": payload_size,
            "status": "ok" if r.ok else "failed",
            "error": (r.error or "")[:200] if not r.ok else "",
            # (b) 新加字段
            "text_preview": text_preview,
            "matched_patterns": matched_patterns,
        })

    return {
        "ok": any_ok,
        "channels": [asdict(r) for r in results],
        "audited": True,
        "priority": priority,
    }
