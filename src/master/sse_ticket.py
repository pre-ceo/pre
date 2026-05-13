"""SSE ticket — 短 TTL one-time-use 凭证.

EventSource 没法带 Authorization header, 只能走 query string. 直接把 raw token
塞进 query 会被 access_log / audit 持久化, 触红线 (token 不进 transcript / 日志).
本模块发一次性 ticket 给 EventSource 当 ?ticket=, raw token 只在 caller 侧停留.

约束:
  - 颁发前 caller 必须已通过 _check_auth (Bearer 路径)
  - 单次使用 (consume 后立即作废) — 不需要; 改 peek (验证不消费). 一个 EventSource
    重连同一 ticket 可能在 EventSource 库内部多次握手 - 不能一击销毁.
    采用: peek (TTL 内重用) + GC 自然过期
  - 8 min TTL, 内存 dict, 不持久化 (master 重启即作废)
  - 绑 caller_token_sha + agent_id, 第三方拿到也只能开同一 agent 同一身份的流
"""

import secrets
import threading
import time
from typing import Optional


TICKET_TTL = 8 * 60  # 8 min
MAX_PER_CALLER = 1_000_000   # 本机使用, ticket 并发上限放开

_TICKETS: dict[str, dict] = {}   # ticket -> {caller_token_sha, agent_id, expires}
_LOCK = threading.Lock()


def _gc_locked(now: float) -> None:
    expired = [t for t, v in _TICKETS.items() if v["expires"] < now]
    for t in expired:
        _TICKETS.pop(t, None)


def issue(caller_token_sha: str, agent_id: str) -> str:
    """颁发 ticket, 返 raw. raise RuntimeError 如果超 MAX_PER_CALLER."""
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        own = sum(1 for v in _TICKETS.values() if v["caller_token_sha"] == caller_token_sha)
        if own >= MAX_PER_CALLER:
            raise RuntimeError(f"too_many_active_tickets:{own}/{MAX_PER_CALLER}")
        ticket = secrets.token_urlsafe(32)
        _TICKETS[ticket] = {
            "caller_token_sha": caller_token_sha,
            "agent_id": agent_id,
            "expires": now + TICKET_TTL,
        }
    return ticket


def peek(ticket: str, agent_id: str) -> Optional[dict]:
    """验证 ticket 但不销毁 (在 TTL 内可多次握手). 返 {caller_token_sha, agent_id} 或 None."""
    if not ticket:
        return None
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        rec = _TICKETS.get(ticket)
        if not rec:
            return None
        if rec["expires"] < now:
            _TICKETS.pop(ticket, None)
            return None
        if rec["agent_id"] != agent_id:
            return None
        return {
            "caller_token_sha": rec["caller_token_sha"],
            "agent_id": rec["agent_id"],
        }


def revoke(ticket: str) -> None:
    """手动作废 (caller 主动登出场景, 可选)."""
    with _LOCK:
        _TICKETS.pop(ticket, None)


def active_count(caller_token_sha: str) -> int:
    """debug / metric."""
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        return sum(1 for v in _TICKETS.values() if v["caller_token_sha"] == caller_token_sha)
