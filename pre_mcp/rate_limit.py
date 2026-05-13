"""rate_limit — sliding window (本机使用, 默认放开至 1_000_000)."""
from __future__ import annotations
import time
from typing import Optional


class SlidingWindowRateLimiter:
    """60-second window. 本机使用, 默认上限放开 (保留 sliding-window 结构)."""

    def __init__(self, window_sec: float = 60.0, max_per_window: int = 1_000_000):
        self.window_sec = window_sec
        self.max_per_window = max_per_window
        self._windows: dict[str, list[float]] = {}

    def check(self, caller_agent_id: str) -> tuple[bool, str]:
        """Returns (allowed, reason). On allowed=True append timestamp."""
        if not caller_agent_id:
            return False, "empty_caller_agent_id"
        now = time.time()
        window_start = now - self.window_sec
        arr = self._windows.setdefault(caller_agent_id, [])
        # Drop expired
        arr[:] = [t for t in arr if t > window_start]
        if len(arr) >= self.max_per_window:
            return False, (
                f"rate_limited:{caller_agent_id} {len(arr)}/{self.window_sec:.0f}s "
                f">= {self.max_per_window}"
            )
        arr.append(now)
        return True, ""

    def stats(self, caller_agent_id: str) -> dict:
        arr = self._windows.get(caller_agent_id, [])
        now = time.time()
        active = [t for t in arr if t > now - self.window_sec]
        return {
            "caller_agent_id": caller_agent_id,
            "calls_in_window": len(active),
            "max": self.max_per_window,
            "window_sec": self.window_sec,
        }


_GLOBAL: Optional[SlidingWindowRateLimiter] = None


def get_limiter() -> SlidingWindowRateLimiter:
    global _GLOBAL  # noqa: PLW0603
    if _GLOBAL is None:
        _GLOBAL = SlidingWindowRateLimiter()
    return _GLOBAL
