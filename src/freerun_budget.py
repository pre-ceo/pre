"""
freerun_budget — freerun task budget enforcer ( / HC-G10).

用途:
  freerun task 必预设 budget cap (max_tokens / max_cost_usd / max_runtime_min /
  llm_calls_per_day_cap). 超 cap → 强制 stop + finding HIGH-budget-exceeded.
  防 freerun 长跑耗尽 LLM quota ( task_summary_loop 60s × 16 agent
  事故的预防).

API:
  load_budget(task_id) -> dict # 取 task budget 配置
  check_budget(task_id, usage) -> (status, reason) # status ∈ {ok, warn, exceeded}
  record_usage(task_id, delta) -> dict # 累计本次 usage, 返新 usage
  reset_daily_counters() -> int # 重置 llm_calls 日计数

 引入.
HC-PRE-1 stdlib only + HC-PRE-2 fail-safe.
"""
from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from common.paths import PRE_RULE_ROOT, PRE_LOG_ROOT, PRE_AGENT_HOME
from typing import Optional

_BUDGETS_PATH = Path(os.environ.get(
    "PRE_FREERUN_BUDGETS",
    str(Path(PRE_RULE_ROOT) / "freerun" / "budgets.json"),
))
_USAGE_STATE_PATH = Path(os.environ.get(
    "PRE_FREERUN_USAGE_STATE",
    str(Path(PRE_LOG_ROOT) / "freerun" / "usage_state.json"),
))


_DEFAULT_BUDGET = {
    "max_tokens": 500000,
    "max_cost_usd": 5.00,
    "max_runtime_min": 30,
    "llm_calls_per_day_cap": 100,
}


def load_budget(task_id: str) -> dict:
    """读 budgets.json, 返该 task budget. 不存在用 default. fail-safe → default."""
    try:
        if not _BUDGETS_PATH.exists():
            return dict(_DEFAULT_BUDGET)
        with open(_BUDGETS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        default_b = cfg.get("default_budget") or _DEFAULT_BUDGET
        task_b = (cfg.get("tasks") or {}).get(task_id) or {}
        out = dict(default_b)
        out.update(task_b)
        return out
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_BUDGET)


def _load_usage_state() -> dict:
    """{task_id: {tokens, cost_usd, runtime_min, llm_calls_today, day, started_ts}}.
    fail-safe → 空 dict."""
    try:
        if not _USAGE_STATE_PATH.exists():
            return {}
        with open(_USAGE_STATE_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_usage_state(state: dict):
    try:
        _USAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_USAGE_STATE_PATH.parent), 0o700)
        except OSError:
            pass
        with open(_USAGE_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        try:
            os.chmod(str(_USAGE_STATE_PATH), 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def record_usage(task_id: str, tokens: int = 0, cost_usd: float = 0.0,
                  runtime_min: float = 0.0, llm_calls: int = 0) -> dict:
    """累计 task usage. tokens/cost/runtime/calls 为本次 delta. 返新 usage 字典."""
    state = _load_usage_state()
    a = state.setdefault(task_id, {
        "tokens": 0, "cost_usd": 0.0, "runtime_min": 0.0,
        "llm_calls_today": 0, "day": _today_str(),
        "started_ts": time.time(),
    })
    today = _today_str()
    if a.get("day") != today:
        # 跨天 reset llm_calls_today (但保留累计 tokens / cost / runtime)
        a["llm_calls_today"] = 0
        a["day"] = today
    a["tokens"] += int(tokens)
    a["cost_usd"] += float(cost_usd)
    a["runtime_min"] += float(runtime_min)
    a["llm_calls_today"] += int(llm_calls)
    state[task_id] = a
    _save_usage_state(state)
    return a


def check_budget(task_id: str, usage: Optional[dict] = None) -> tuple[str, str]:
    """检查 task 是否超 budget. 返 (status, reason).
    status ∈ {ok, warn, exceeded}.
    - exceeded: 任意维度超 cap → 上层应 stop task + finding HIGH
    - warn: ≥80% cap → 软警告 (上层可 alert)
    - ok: 全在 budget 内
    fail-safe: 任何异常 → ok (不强制 stop, 但通常 record_usage 也会失败).
    """
    try:
        budget = load_budget(task_id)
        if usage is None:
            state = _load_usage_state()
            usage = state.get(task_id) or {}

        tokens = usage.get("tokens", 0)
        cost = usage.get("cost_usd", 0.0)
        runtime = usage.get("runtime_min", 0.0)
        calls = usage.get("llm_calls_today", 0)

        # exceeded checks
        if tokens >= budget["max_tokens"]:
            return "exceeded", f"max_tokens {tokens}/{budget['max_tokens']}"
        if cost >= budget["max_cost_usd"]:
            return "exceeded", f"max_cost_usd {cost:.2f}/{budget['max_cost_usd']:.2f}"
        if runtime >= budget["max_runtime_min"]:
            return "exceeded", f"max_runtime_min {runtime:.1f}/{budget['max_runtime_min']}"
        if calls >= budget["llm_calls_per_day_cap"]:
            return "exceeded", (f"llm_calls_per_day_cap {calls}/"
                                  f"{budget['llm_calls_per_day_cap']}")

        # warn checks (80% threshold)
        if tokens >= 0.8 * budget["max_tokens"]:
            return "warn", f"max_tokens 80%: {tokens}/{budget['max_tokens']}"
        if cost >= 0.8 * budget["max_cost_usd"]:
            return "warn", f"max_cost_usd 80%: {cost:.2f}/{budget['max_cost_usd']:.2f}"
        if runtime >= 0.8 * budget["max_runtime_min"]:
            return "warn", f"max_runtime_min 80%: {runtime:.1f}/{budget['max_runtime_min']}"
        if calls >= 0.8 * budget["llm_calls_per_day_cap"]:
            return "warn", (f"llm_calls_per_day_cap 80%: {calls}/"
                              f"{budget['llm_calls_per_day_cap']}")

        return "ok", ""
    except Exception:
        return "ok", "exception_fallthrough"


def reset_daily_counters() -> int:
    """跨天 reset 所有 task 的 llm_calls_today. 返 reset 个数."""
    state = _load_usage_state()
    today = _today_str()
    n = 0
    for tid, a in state.items():
        if a.get("day") != today:
            a["llm_calls_today"] = 0
            a["day"] = today
            n += 1
    if n > 0:
        _save_usage_state(state)
    return n


def write_budget_finding(task_id: str, reason: str,
                          findings_dir: Optional[str] = None) -> Optional[Path]:
    """超 cap 时写 finding HIGH-budget-exceeded-{task_id}.md 触发 stop.
    findings_dir 默认从 task_id 推导 (e.g. local.cli-claude-code-local.agent-research → <PRE_AGENT_HOME>/agent-research/pre/findings/)."""
    if findings_dir:
        target_dir = Path(findings_dir)
    else:
        # 推导: task_id 末段是 project name
        last = task_id.split(".")[-1] if "." in task_id else task_id
        target_dir = Path(PRE_AGENT_HOME) / last / "pre" / "findings"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        finding_path = target_dir / f"HIGH-budget-exceeded-{task_id}.md"
        if finding_path.exists():
            return finding_path  # 已存在不重写
        with open(finding_path, "w", encoding="utf-8") as f:
            f.write(f"# HIGH: budget exceeded — {task_id}\n\n")
            f.write(f"- ts: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"- reason: {reason}\n\n")
            f.write("Budget cap reached. Task should stop. Reset usage state via:\n\n")
            f.write(f"```\n# 删除 $PRE_LOG_DIR/freerun/usage_state.json '{task_id}' 字段\n```\n")
        return finding_path
    except OSError:
        return None
