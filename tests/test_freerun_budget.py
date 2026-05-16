"""freerun_budget.py — task budget cap enforcer.

覆盖:
  - load_budget: 配置缺 → default, task-specific override default
  - record_usage 累加 tokens/cost/runtime/llm_calls
  - check_budget: ok / warn (≥80%) / exceeded (≥100%) 各维度
  - 跨天 reset llm_calls_today, 保留累计 tokens
  - write_budget_finding 写 HIGH-budget-exceeded-*.md
"""
from __future__ import annotations
import importlib
import json
import os
from pathlib import Path

import pytest


def _fresh_budget(tmp_path):
    """指向 isolated paths, 重 import freerun_budget."""
    budgets = tmp_path / "budgets.json"
    usage_state = tmp_path / "usage_state.json"
    os.environ["PRE_FREERUN_BUDGETS"] = str(budgets)
    os.environ["PRE_FREERUN_USAGE_STATE"] = str(usage_state)

    import sys
    sys.modules.pop("freerun_budget", None)
    return importlib.import_module("freerun_budget"), budgets, usage_state


def test_default_budget_when_no_config(tmp_path):
    fb, _budgets, _ = _fresh_budget(tmp_path)
    b = fb.load_budget("task-1")
    assert b == fb._DEFAULT_BUDGET
    assert b["max_tokens"] == 500000


def test_task_override_merges_with_default(tmp_path):
    fb, budgets, _ = _fresh_budget(tmp_path)
    budgets.write_text(json.dumps({
        "default_budget": {"max_tokens": 1000, "max_cost_usd": 1.0,
                            "max_runtime_min": 5, "llm_calls_per_day_cap": 10},
        "tasks": {"task-A": {"max_tokens": 5000}},
    }))
    b = fb.load_budget("task-A")
    assert b["max_tokens"] == 5000
    assert b["max_cost_usd"] == 1.0  # 继承 default


def test_record_usage_accumulates(tmp_path):
    fb, _b, _u = _fresh_budget(tmp_path)
    u1 = fb.record_usage("t1", tokens=100, cost_usd=0.1, runtime_min=1.0, llm_calls=2)
    assert u1["tokens"] == 100
    u2 = fb.record_usage("t1", tokens=50, llm_calls=1)
    assert u2["tokens"] == 150
    assert u2["llm_calls_today"] == 3
    assert u2["cost_usd"] == pytest.approx(0.1)


def test_check_budget_ok_under_threshold(tmp_path):
    fb, budgets, _ = _fresh_budget(tmp_path)
    budgets.write_text(json.dumps({
        "default_budget": {"max_tokens": 1000, "max_cost_usd": 1.0,
                            "max_runtime_min": 60, "llm_calls_per_day_cap": 100},
    }))
    fb.record_usage("t1", tokens=100)
    status, _ = fb.check_budget("t1")
    assert status == "ok"


def test_check_budget_warn_at_80pct(tmp_path):
    fb, budgets, _ = _fresh_budget(tmp_path)
    budgets.write_text(json.dumps({
        "default_budget": {"max_tokens": 1000, "max_cost_usd": 1.0,
                            "max_runtime_min": 60, "llm_calls_per_day_cap": 100},
    }))
    fb.record_usage("t1", tokens=800)
    status, reason = fb.check_budget("t1")
    assert status == "warn"
    assert "max_tokens 80%" in reason


def test_check_budget_exceeded(tmp_path):
    fb, budgets, _ = _fresh_budget(tmp_path)
    budgets.write_text(json.dumps({
        "default_budget": {"max_tokens": 1000, "max_cost_usd": 1.0,
                            "max_runtime_min": 60, "llm_calls_per_day_cap": 100},
    }))
    fb.record_usage("t1", tokens=1500)
    status, reason = fb.check_budget("t1")
    assert status == "exceeded"
    assert "max_tokens" in reason


def test_check_budget_with_explicit_usage(tmp_path):
    """显式传 usage 字典 (绕开 file 读)."""
    fb, _b, _u = _fresh_budget(tmp_path)
    status, _ = fb.check_budget("t1", usage={"cost_usd": 100.0})
    assert status == "exceeded"


def test_check_budget_unknown_task_ok(tmp_path):
    fb, _b, _u = _fresh_budget(tmp_path)
    status, _ = fb.check_budget("never-recorded")
    assert status == "ok"


def test_reset_daily_counters_when_day_rolls(tmp_path):
    fb, _b, usage = _fresh_budget(tmp_path)
    usage.write_text(json.dumps({
        "t1": {"tokens": 100, "cost_usd": 0, "runtime_min": 0,
                "llm_calls_today": 50, "day": "19700101", "started_ts": 0},
        "t2": {"tokens": 200, "cost_usd": 0, "runtime_min": 0,
                "llm_calls_today": 10, "day": fb._today_str(), "started_ts": 0},
    }))
    n = fb.reset_daily_counters()
    assert n == 1  # 只 t1 跨天
    state = json.loads(usage.read_text())
    assert state["t1"]["llm_calls_today"] == 0
    assert state["t1"]["tokens"] == 100  # tokens 不重置
    assert state["t2"]["llm_calls_today"] == 10  # 同天不动


def test_write_budget_finding_creates_file(tmp_path):
    fb, _b, _u = _fresh_budget(tmp_path)
    findings_dir = tmp_path / "findings"
    p = fb.write_budget_finding("local.test.task-1", "max_tokens 1500/1000",
                                 findings_dir=str(findings_dir))
    assert p is not None
    assert p.exists()
    content = p.read_text()
    assert "HIGH" in content
    assert "max_tokens 1500/1000" in content


def test_write_budget_finding_idempotent(tmp_path):
    fb, _b, _u = _fresh_budget(tmp_path)
    findings_dir = tmp_path / "findings"
    p1 = fb.write_budget_finding("t-1", "r1", findings_dir=str(findings_dir))
    p2 = fb.write_budget_finding("t-1", "r2", findings_dir=str(findings_dir))
    assert p1 == p2
    # 不覆盖原内容
    assert "r1" in p1.read_text()
    assert "r2" not in p1.read_text()
