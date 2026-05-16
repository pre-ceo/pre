"""pre_mcp/rate_limit.py — SlidingWindowRateLimiter 单元测试.

覆盖:
  - 空 caller_agent_id → 拒绝
  - 限内通过 + 计数累积
  - 触顶 → 拒绝, reason 含 caller id
  - 窗口滑出 → 旧记录回收, 再次通过
  - stats() 返当前 in-window 计数
  - get_limiter() 单例幂等
"""
from __future__ import annotations
import time

import pytest

from rate_limit import SlidingWindowRateLimiter, get_limiter


def test_empty_caller_id_rejected():
    rl = SlidingWindowRateLimiter()
    ok, reason = rl.check("")
    assert ok is False
    assert reason == "empty_caller_agent_id"


def test_under_cap_allows_and_appends():
    rl = SlidingWindowRateLimiter(window_sec=60.0, max_per_window=3)
    for _ in range(3):
        ok, reason = rl.check("agent-A")
        assert ok is True
        assert reason == ""
    # 第 4 次触顶
    ok, reason = rl.check("agent-A")
    assert ok is False
    assert "rate_limited:agent-A" in reason
    assert "3/60s" in reason


def test_different_callers_isolated():
    rl = SlidingWindowRateLimiter(window_sec=60.0, max_per_window=2)
    assert rl.check("agent-A")[0] is True
    assert rl.check("agent-A")[0] is True
    # agent-A 已触顶
    assert rl.check("agent-A")[0] is False
    # agent-B 独立窗口
    assert rl.check("agent-B")[0] is True


def test_window_expires(monkeypatch):
    """老时间戳被滑出窗口后, 新调用应通过."""
    rl = SlidingWindowRateLimiter(window_sec=1.0, max_per_window=2)
    t0 = 1000.0
    monkeypatch.setattr(time, "time", lambda: t0)
    assert rl.check("agent-A")[0] is True
    assert rl.check("agent-A")[0] is True
    assert rl.check("agent-A")[0] is False  # 触顶

    # 时间推到窗口外
    monkeypatch.setattr(time, "time", lambda: t0 + 2.0)
    ok, _ = rl.check("agent-A")
    assert ok is True


def test_stats_reports_in_window_count():
    rl = SlidingWindowRateLimiter(window_sec=60.0, max_per_window=5)
    rl.check("agent-A")
    rl.check("agent-A")
    s = rl.stats("agent-A")
    assert s["calls_in_window"] == 2
    assert s["max"] == 5
    assert s["window_sec"] == 60.0
    assert s["caller_agent_id"] == "agent-A"


def test_stats_unknown_caller_returns_zero():
    rl = SlidingWindowRateLimiter()
    s = rl.stats("never-seen")
    assert s["calls_in_window"] == 0


def test_get_limiter_is_singleton():
    a = get_limiter()
    b = get_limiter()
    assert a is b
