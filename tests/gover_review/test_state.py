"""state.py — 周期状态机 + 持久化单测.

覆盖:
  floor_to_period      wall-clock 边界对齐 (00/04/08/12/16/20 UTC)
  init_state           fresh schema 齐全
  load                 missing/bad json/non-dict/缺字段 → fresh / 补齐
  save                 写出 valid json, 往返 load 等值
  save 原子            mkstemp + replace, 半路 raise 不留半文件
  is_pending           None/空字串 → False, 非空 → True
  should_run_cycle     pending/未到下一边界/有新数据/无 last_end fresh
  enter_waiting        pending_* 字段 set
  complete_cycle       cycle_n+=1, last_end=until, pending 全清
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gover_review.state import (
    PERIOD_SECONDS,
    STATE_KEYS,
    complete_cycle,
    enter_waiting,
    floor_to_period,
    init_state,
    is_pending,
    load,
    save,
    should_run_cycle,
)


def dt(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# ---------- floor_to_period ----------

def test_floor_to_period_exact_boundary_unchanged():
    t = dt(2026, 5, 17, 8, 0, 0)
    assert floor_to_period(t) == t


def test_floor_to_period_between_boundaries():
    t = dt(2026, 5, 17, 10, 35, 17)
    assert floor_to_period(t) == dt(2026, 5, 17, 8, 0, 0)


def test_floor_to_period_just_after_boundary():
    t = dt(2026, 5, 17, 12, 0, 1)
    assert floor_to_period(t) == dt(2026, 5, 17, 12, 0, 0)


def test_floor_to_period_lands_on_canonical_boundary():
    """4h 边界应严格落在 00/04/08/12/16/20 UTC."""
    for h in range(24):
        t = dt(2026, 5, 17, h, 30)
        b = floor_to_period(t)
        assert b.hour in (0, 4, 8, 12, 16, 20)
        assert b.minute == 0 and b.second == 0


def test_floor_to_period_custom_period():
    t = dt(2026, 5, 17, 10, 35)
    b = floor_to_period(t, period_seconds=3600)  # 1h
    assert b == dt(2026, 5, 17, 10, 0)


# ---------- init_state ----------

def test_init_state_schema_complete():
    s = init_state(now=dt(2026, 5, 17, 10, 0))
    for k in STATE_KEYS:
        assert k in s
    assert s["cycle_n"] == 0
    assert s["pending_finding_path"] is None


def test_init_state_last_end_aligned_to_boundary():
    s = init_state(now=dt(2026, 5, 17, 10, 35))
    assert s["last_cycle_end_ts"] == "2026-05-17T08:00:00+00:00"


# ---------- load / save ----------

def test_load_missing_file_returns_fresh(tmp_path):
    s = load(tmp_path / "absent.json", now=dt(2026, 5, 17, 10, 0))
    assert s["cycle_n"] == 0
    assert s["last_cycle_end_ts"] == "2026-05-17T08:00:00+00:00"


def test_load_bad_json_returns_fresh(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    s = load(p, now=dt(2026, 5, 17, 10, 0))
    assert s["cycle_n"] == 0


def test_load_non_dict_returns_fresh(tmp_path):
    p = tmp_path / "list.json"
    p.write_text("[1,2,3]")
    s = load(p, now=dt(2026, 5, 17, 10, 0))
    assert s["cycle_n"] == 0


def test_load_missing_fields_filled(tmp_path):
    p = tmp_path / "partial.json"
    p.write_text('{"cycle_n": 5}')
    s = load(p, now=dt(2026, 5, 17, 10, 0))
    assert s["cycle_n"] == 5
    for k in STATE_KEYS:
        assert k in s


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    s = init_state(now=dt(2026, 5, 17, 8, 0))
    s["cycle_n"] = 3
    s["pending_finding_path"] = "/foo/INFO.md"
    save(p, s)
    loaded = load(p)
    assert loaded == s


def test_save_creates_parent_dir(tmp_path):
    p = tmp_path / "deep" / "nested" / "state.json"
    save(p, init_state(now=dt(2026, 5, 17, 8, 0)))
    assert p.exists()


def test_save_atomic_no_temp_files_left(tmp_path):
    p = tmp_path / "state.json"
    save(p, init_state(now=dt(2026, 5, 17, 8, 0)))
    save(p, init_state(now=dt(2026, 5, 17, 12, 0)))
    leftover = [x for x in tmp_path.iterdir() if x.name.startswith(".state.")]
    assert leftover == []


def test_save_atomic_failure_does_not_corrupt_existing(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    save(p, init_state(now=dt(2026, 5, 17, 8, 0)))
    original = p.read_text()

    # 强制 os.replace raise → 原文件不应被损坏
    import gover_review.state as state_mod

    def boom(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(state_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save(p, init_state(now=dt(2026, 5, 17, 12, 0)))
    assert p.read_text() == original
    # 失败的 tmp 文件不应残留
    leftover = [x for x in tmp_path.iterdir() if x.name.startswith(".state.")]
    assert leftover == []


# ---------- is_pending ----------

def test_is_pending_none_false():
    assert is_pending(init_state(now=dt(2026, 5, 17, 10, 0))) is False


def test_is_pending_empty_string_false():
    s = init_state(now=dt(2026, 5, 17, 10, 0))
    s["pending_finding_path"] = ""
    assert is_pending(s) is False


def test_is_pending_path_true():
    s = init_state(now=dt(2026, 5, 17, 10, 0))
    s["pending_finding_path"] = "/foo/INFO.md"
    assert is_pending(s) is True


# ---------- should_run_cycle ----------

def test_should_run_cycle_when_pending_returns_none():
    s = init_state(now=dt(2026, 5, 17, 8, 0))
    s["pending_finding_path"] = "/foo/INFO.md"
    assert should_run_cycle(s, now=dt(2026, 5, 17, 12, 1)) is None


def test_should_run_cycle_within_same_period_returns_none():
    # last_end=08:00, now=10:30 → until=08:00, since=08:00, 空窗
    s = init_state(now=dt(2026, 5, 17, 8, 0))  # last_end=08:00
    assert should_run_cycle(s, now=dt(2026, 5, 17, 10, 30)) is None


def test_should_run_cycle_crosses_boundary_returns_window():
    s = init_state(now=dt(2026, 5, 17, 8, 0))  # last_end=08:00
    out = should_run_cycle(s, now=dt(2026, 5, 17, 12, 0, 5))
    assert out is not None
    since, until = out
    assert since == dt(2026, 5, 17, 8, 0)
    assert until == dt(2026, 5, 17, 12, 0)


def test_should_run_cycle_missed_multiple_periods_one_window():
    """关机 8h, cron 重启 → since=08:00, until=16:00 一次审完."""
    s = init_state(now=dt(2026, 5, 17, 8, 0))  # last_end=08:00
    out = should_run_cycle(s, now=dt(2026, 5, 17, 16, 0, 30))
    assert out is not None
    since, until = out
    assert since == dt(2026, 5, 17, 8, 0)
    assert until == dt(2026, 5, 17, 16, 0)


def test_should_run_cycle_no_last_end_field_defaults():
    """state 缺 last_cycle_end_ts → fallback 到 now - period."""
    s = {"pending_finding_path": None}
    out = should_run_cycle(s, now=dt(2026, 5, 17, 12, 0, 30))
    assert out is not None


# ---------- enter_waiting ----------

def test_enter_waiting_sets_pending_fields():
    s = init_state(now=dt(2026, 5, 17, 12, 0))
    s2 = enter_waiting(
        s,
        finding_path="/p/INFO.md",
        sha256="abc",
        now=dt(2026, 5, 17, 12, 5),
    )
    assert s2["pending_finding_path"] == "/p/INFO.md"
    assert s2["pending_sha256"] == "abc"
    assert s2["pending_since_ts"] == "2026-05-17T12:05:00+00:00"
    # 原 state 不应被 mutate
    assert s["pending_finding_path"] is None


# ---------- complete_cycle ----------

def test_complete_cycle_increments_and_clears():
    s = init_state(now=dt(2026, 5, 17, 8, 0))
    s["cycle_n"] = 4
    s["pending_finding_path"] = "/p/INFO.md"
    s["pending_sha256"] = "abc"
    s["pending_since_ts"] = "2026-05-17T12:00:00+00:00"
    s2 = complete_cycle(s, until=dt(2026, 5, 17, 12, 0))
    assert s2["cycle_n"] == 5
    assert s2["last_cycle_end_ts"] == "2026-05-17T12:00:00+00:00"
    assert s2["pending_finding_path"] is None
    assert s2["pending_sha256"] is None
    assert s2["pending_since_ts"] is None


def test_complete_cycle_from_default_zero():
    s = {}
    s2 = complete_cycle(s, until=dt(2026, 5, 17, 12, 0))
    assert s2["cycle_n"] == 1


# ---------- full state machine integration ----------

def test_full_state_machine_idle_review_wait_complete(tmp_path):
    """跑一遍 IDLE → REVIEWING → WAITING_USER → IDLE 整链, 落盘往返."""
    p = tmp_path / "state.json"
    s = load(p, now=dt(2026, 5, 17, 12, 0, 5))

    # IDLE: cron tick, 应跑 review
    out = should_run_cycle(s, now=dt(2026, 5, 17, 16, 0, 5))
    assert out is not None
    since, until = out

    # REVIEWING → WAITING_USER
    s = enter_waiting(
        s,
        finding_path=str(tmp_path / "INFO.md"),
        sha256="xx",
        now=dt(2026, 5, 17, 16, 1),
    )
    save(p, s)

    # cron 再 tick → 应 skip (pending)
    s_reload = load(p)
    assert should_run_cycle(s_reload, now=dt(2026, 5, 17, 20, 0, 5)) is None

    # 用户答完 → complete
    s_done = complete_cycle(s_reload, until=until)
    save(p, s_done)

    s_final = load(p)
    assert s_final["cycle_n"] == 1
    assert s_final["pending_finding_path"] is None
    assert s_final["last_cycle_end_ts"] == until.isoformat()
