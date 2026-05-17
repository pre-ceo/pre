"""gover_review 周期状态机 + state.json 持久化.

state schema (pre_rule/state/gover_review.json 或 ~/.pre/state/gover_review.json):
  {
    "cycle_n": int,                  # 已完成的 cycle 数
    "last_cycle_end_ts": ISO|None,   # 上次 cycle 结束时间 (= 下次窗口 since)
    "pending_finding_path": str|None, # 非空 = WAITING_USER
    "pending_since_ts": ISO|None,
    "pending_sha256": str|None,
  }

状态转移:
  IDLE                                            (pending_finding_path == None)
    | should_run_cycle() -> (since, until)
    v
  REVIEWING (in-memory, 不持久化)
    | enter_waiting(finding_path, sha256)
    v
  WAITING_USER                                    (pending_finding_path != None)
    | (cron tick) should_run_cycle() -> None      # skip 本轮
    | (user fills) complete_cycle(until)
    v
  IDLE                                            (cycle_n += 1, last_cycle_end_ts = until)

wall-clock 对齐: floor_to_period(now, 14400) 给出最近一个 00/04/08/12/16/20 UTC 边界.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PERIOD_SECONDS = 14400  # 4h

STATE_KEYS = (
    "cycle_n",
    "last_cycle_end_ts",
    "pending_finding_path",
    "pending_since_ts",
    "pending_sha256",
)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(s2)
    except ValueError:
        return None


def floor_to_period(
    t: datetime, period_seconds: int = PERIOD_SECONDS
) -> datetime:
    """t 向下取整到 wall-clock UTC period 整数倍.

    period_seconds=14400 时, 边界正好落在 00/04/08/12/16/20 UTC (因 epoch=1970-01-01).
    """
    epoch = int(t.astimezone(timezone.utc).timestamp())
    floored = (epoch // period_seconds) * period_seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def init_state(now: datetime | None = None) -> dict:
    """fresh state. last_cycle_end_ts = 上一个 wall-clock 边界."""
    if now is None:
        now = datetime.now(timezone.utc)
    last_end = floor_to_period(now)
    return {
        "cycle_n": 0,
        "last_cycle_end_ts": last_end.isoformat(),
        "pending_finding_path": None,
        "pending_since_ts": None,
        "pending_sha256": None,
    }


def load(path: Path | str, now: datetime | None = None) -> dict:
    """读 state. 缺失/坏 json/非 dict → fresh init_state. 缺字段 → 补齐."""
    p = Path(path)
    fresh = init_state(now)
    if not p.exists():
        return fresh
    try:
        with open(p) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return fresh
    if not isinstance(data, dict):
        return fresh
    for k in STATE_KEYS:
        data.setdefault(k, fresh[k])
    return data


def save(path: Path | str, state: dict) -> None:
    """原子写: 同目录 mkstemp + os.replace, 防止半文件."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_pending(state: dict) -> bool:
    return bool(state.get("pending_finding_path"))


def should_run_cycle(
    state: dict,
    *,
    now: datetime | None = None,
    period_seconds: int = PERIOD_SECONDS,
) -> tuple[datetime, datetime] | None:
    """cron tick 决策入口. 返 (since, until) 或 None (skip).

    None 情况:
      - pending → 还在等用户答, 不开新 cycle
      - until <= last → 还没到下个 wall-clock 边界
    """
    if is_pending(state):
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    last = _parse_ts(state.get("last_cycle_end_ts"))
    if last is None:
        last = floor_to_period(
            now - timedelta(seconds=period_seconds), period_seconds
        )
    until = floor_to_period(now, period_seconds)
    if until <= last:
        return None
    return (last, until)


def enter_waiting(
    state: dict,
    *,
    finding_path: str,
    sha256: str,
    now: datetime | None = None,
) -> dict:
    """REVIEWING → WAITING_USER. 返新 dict (不 mutate 原)."""
    if now is None:
        now = datetime.now(timezone.utc)
    new = dict(state)
    new["pending_finding_path"] = finding_path
    new["pending_sha256"] = sha256
    new["pending_since_ts"] = now.isoformat()
    return new


def complete_cycle(
    state: dict,
    *,
    until: datetime,
) -> dict:
    """WAITING_USER → IDLE (用户答完). cycle_n+=1, last_cycle_end_ts=until, 清 pending."""
    new = dict(state)
    new["cycle_n"] = int(state.get("cycle_n", 0)) + 1
    new["last_cycle_end_ts"] = until.isoformat()
    new["pending_finding_path"] = None
    new["pending_since_ts"] = None
    new["pending_sha256"] = None
    return new
