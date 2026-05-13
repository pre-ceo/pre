"""
conversation_lifecycle — fn_runtime phase 2.

会话生命周期: bus 派的任务 (mini_task 带 parent_dispatch_id) 完成时,
默认 /clear 释放 context, 距上次清理 ≥ N 秒 (默认 2h) 则 /compact 保留摘要.

**严禁基于 transcript 字节数 / 字符长度判断** (user 反馈).
触发以"任务"为单位, 在 stop hook (stop_analyzer) 中判断.

仅 fn_* infrastructure agent 范围, 业务 agent (PM2 自管) 严禁接管 ().

事件触发 (HC-A9/G10): stop_analyzer 推完 mini_task 后调 auto_evaluate(agent_id, mini_task), 不轮询.
HC-PRE-1 stdlib only. 0 LLM cost (仅 fs read + tmux send-keys).
HC-PRE-cron-2 mtime hot reload.

API:
  load_config() -> dict
  list_targets(only_enabled=False) -> list[str]
  is_excluded(agent_id) -> bool
  health(agent_id) -> dict
  should_act(agent_id, mini_task=None, force=False) -> dict {action, reason, ...}
  clear(agent_id, initiated_by, force=False) -> dict
  compact(agent_id, initiated_by, force=False) -> dict
  auto_evaluate(agent_id, mini_task=None, initiated_by="stop_analyzer") -> dict

action ∈ {clear, compact, noop}.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from common.paths import PRE_RULE_ROOT, PRE_LOG_ROOT
from typing import Optional

# Loopback master call: direct, bypass proxy env (Surge etc.)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 复用 tmux_helper
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from tmux_helper import has_session, capture_pane, find_tmux, send_key, send_to_tmux
except ImportError:
    def has_session(s, timeout=3.0): return False
    def capture_pane(s, lines=10, timeout=3.0): return ""
    def find_tmux(): return shutil.which("tmux") or "tmux"
    def send_key(s, k, timeout=3.0): return False
    def send_to_tmux(s, t, timeout=5.0, max_retry=1): return False


# 路径常量
_RULE_PATH = Path(os.environ.get(
    "PRE_CONVERSATION_LIFECYCLE",
    str(Path(PRE_RULE_ROOT) / "runtime" / "conversation_lifecycle.json"),
))
_LOG_DIR = Path(os.environ.get(
    "PRE_LOG_DIR",
    PRE_LOG_ROOT,
))
_RUNTIME_LOG_DIR = _LOG_DIR / "runtime"
_STATE_DIR = _RUNTIME_LOG_DIR / "conversation_state"

_MASTER_URL = os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500").rstrip("/")
# token: lazy resolve from ~/.pre/env via token_resolver (PR3).
# 不预存常量; 失败时上游 except 兜底.
try:
    from src.common.token_resolver import resolve as _resolve_token  # hook context
except ImportError:
    from common.token_resolver import resolve as _resolve_token  # master context

# config cache (mtime hot reload)
_CACHE: dict = {"mtime": 0.0, "cfg": None}

# 默认配置 (config 缺失时 fail-safe).
# 严禁字节阈值 (user ): 触发以任务为单位.
_DEFAULT_CONFIG = {
    "version": 1,
    "schema_doc": (
        "fn_runtime conversation_lifecycle (, v1.1). "
        "MH-6 scope 严守 fn_* infrastructure only, 业务 agent (batpm/agent-trade/...) PM2 自管严禁接管. "
        "触发条件: bus 派的任务完成 (mini_task 带 parent_dispatch_id), 不基于 transcript 长度."
    ),
    "enabled": False,
    "thresholds": {
        # /compact vs /clear 的选择: 距上次清理超 N 秒 → /compact, 否则 /clear.
        # 任务级时间窗口 (非字节). 默认 2h.
        "auto_compact_after_seconds": 7200,
        # 同 agent N 秒内不重复触发 (防 stop_hook 重复 fire 同一 mini_task).
        "cooldown_seconds": 30,
        # 是否要求 mini_task 带 parent_dispatch_id 才触发. 默认 True.
        "require_parent_dispatch_id": True,
    },
    "include_agents": [],
    "exclude_agents_substring": [
        # 业务 agent (PM2 自管) 严禁接管, 永久排除
        "batpm", "agent-trade", "fox", "agent-research", "weasel", "owl", "llama", "raccoon",
    ],
}


def load_config() -> dict:
    """mtime hot reload. fail-safe: config 不可读 → default disabled config."""
    try:
        if not _RULE_PATH.exists():
            return dict(_DEFAULT_CONFIG)
        mtime = _RULE_PATH.stat().st_mtime
        if _CACHE["cfg"] is not None and _CACHE["mtime"] == mtime:
            return _CACHE["cfg"]
        with open(_RULE_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(_DEFAULT_CONFIG)
        merged.update(cfg)
        merged_thr = dict(_DEFAULT_CONFIG["thresholds"])
        merged_thr.update(cfg.get("thresholds") or {})
        merged["thresholds"] = merged_thr
        _CACHE["cfg"] = merged
        _CACHE["mtime"] = mtime
        return merged
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_CONFIG)


def list_targets(only_enabled: bool = False) -> list[str]:
    cfg = load_config()
    inc = cfg.get("include_agents") or []
    if only_enabled and not cfg.get("enabled"):
        return []
    return list(inc)


def is_excluded(agent_id: str) -> bool:
    cfg = load_config()
    excl = cfg.get("exclude_agents_substring") or []
    for sub in excl:
        if sub and sub in agent_id:
            return True
    return False


def is_included(agent_id: str) -> bool:
    cfg = load_config()
    inc = cfg.get("include_agents") or []
    return agent_id in inc


def _state_path(agent_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in agent_id)
    return _STATE_DIR / f"{safe}.json"


def _read_state(agent_id: str) -> dict:
    """state file: {last_action_ts, last_action, tasks_since_cleanup, last_dispatch_id}."""
    p = _state_path(agent_id)
    try:
        if not p.exists():
            return {"last_action_ts": 0.0, "last_action": "",
                    "tasks_since_cleanup": 0, "last_dispatch_id": ""}
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"last_action_ts": 0.0, "last_action": "",
                "tasks_since_cleanup": 0, "last_dispatch_id": ""}


def _write_state(agent_id: str, state: dict):
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_STATE_DIR), 0o700)
        except OSError:
            pass
        p = _state_path(agent_id)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _get_agent_meta(agent_id: str, override: Optional[dict] = None) -> Optional[dict]:
    """从 master /api/v1/agents 拿单 agent. fail-safe → None.

    override: 调用方 (如 master server endpoint) 已有 in-memory meta 时直接传入,
              避免 HTTP self-call deadlock (master 单线程 HTTPServer).
    """
    if override is not None:
        return override
    try:
        req = urllib.request.Request(
            f"{_MASTER_URL}/api/v1/agents",
            headers={"Authorization": f"Bearer {_resolve_token('hook')}"},
        )
        with _NO_PROXY_OPENER.open(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for a in data.get("agents", []):
            if a.get("agent_id") == agent_id:
                return a
    except (urllib.error.URLError, urllib.error.HTTPError,
            OSError, ValueError, RuntimeError):
        return None
    return None


def _resolve_tmux_session(agent_id: str, agent_meta: Optional[dict] = None) -> str:
    if agent_meta is None:
        agent_meta = _get_agent_meta(agent_id) or {}
    md = agent_meta.get("metadata") or {}
    sess = md.get("tmux_session") or ""
    if sess:
        return sess
    parts = agent_id.split(".")
    return parts[-1] if parts else ""


def _has_parent_dispatch(mini_task: Optional[dict]) -> bool:
    """检测 mini_task 是否来自 bus 派的任务 (有 parent_dispatch_id)."""
    if not mini_task:
        return False
    return bool(mini_task.get("parent_dispatch_id"))


def health(agent_id: str, agent_meta_override: Optional[dict] = None) -> dict:
    """检查 agent 适用 conversation_lifecycle 的状态. 0 LLM cost.

    agent_meta_override: master 进程内调用时直接传 in-memory meta, 避 HTTP self-call deadlock.
    """
    cfg = load_config()
    state = _read_state(agent_id)
    out: dict = {
        "agent_id": agent_id,
        "enabled": bool(cfg.get("enabled")),
        "included": is_included(agent_id),
        "excluded": is_excluded(agent_id),
        "tmux_session": "",
        "tmux_alive": False,
        "last_action_ts": state.get("last_action_ts", 0.0),
        "last_action": state.get("last_action", ""),
        "tasks_since_cleanup": state.get("tasks_since_cleanup", 0),
        "last_dispatch_id": state.get("last_dispatch_id", ""),
        "in_cooldown": False,
        "agent_state": None,
    }
    meta = _get_agent_meta(agent_id, override=agent_meta_override)
    if meta:
        out["agent_state"] = (meta.get("activity") or {}).get("state")
        sess = _resolve_tmux_session(agent_id, meta)
        out["tmux_session"] = sess
        out["tmux_alive"] = has_session(sess, timeout=2.0) if sess else False
    cd = cfg.get("thresholds", {}).get("cooldown_seconds", 30)
    out["in_cooldown"] = (time.time() - out["last_action_ts"]) < cd
    return out


def should_act(agent_id: str, mini_task: Optional[dict] = None,
                force: bool = False,
                agent_meta_override: Optional[dict] = None) -> dict:
    """决策 action. 不执行, 仅返 {action, reason, ...}.

    严格不基于 transcript 字节长度. 触发以任务为单位:
      - mini_task 必带 parent_dispatch_id (bus 派的任务) — 否则 noop
      - agent 必 idle — 否则 noop
      - cooldown 内 — noop
      - 距上次清理 ≥ auto_compact_after_seconds → /compact
      - 否则 → /clear
    """
    cfg = load_config()
    thr = cfg.get("thresholds") or {}

    # invariant: excluded 永远是最高优先级硬门槛, 即使 enabled=true 误开,
    # 业务 agent 也永远不被接管 ( phase 3 调整顺序).
    if is_excluded(agent_id):
        return {"action": "noop", "reason": "excluded", "agent_id": agent_id}
    if not cfg.get("enabled") and not force:
        return {"action": "noop", "reason": "disabled", "agent_id": agent_id}
    if not is_included(agent_id) and not force:
        return {"action": "noop", "reason": "not_in_include_list", "agent_id": agent_id}

    # 必须是 bus 派的任务 (有 parent_dispatch_id)
    require_pdi = thr.get("require_parent_dispatch_id", True)
    if require_pdi and not _has_parent_dispatch(mini_task) and not force:
        return {"action": "noop", "reason": "no_parent_dispatch_id",
                "agent_id": agent_id}

    # cooldown 检查
    state = _read_state(agent_id)
    last_ts = state.get("last_action_ts", 0.0)
    cd = thr.get("cooldown_seconds", 30)
    if (time.time() - last_ts) < cd and not force:
        return {"action": "noop", "reason": "in_cooldown",
                "agent_id": agent_id,
                "remaining_sec": int(cd - (time.time() - last_ts))}

    # agent state 检查 (必须 idle)
    meta = _get_agent_meta(agent_id, override=agent_meta_override)
    if not meta:
        return {"action": "noop", "reason": "agent_offline", "agent_id": agent_id}
    agent_state = (meta.get("activity") or {}).get("state")
    if agent_state and agent_state != "idle" and not force:
        return {"action": "noop", "reason": "state_not_idle",
                "agent_id": agent_id, "state": agent_state}

    sess = _resolve_tmux_session(agent_id, meta)
    if not sess or not has_session(sess, timeout=2.0):
        return {"action": "noop", "reason": "missing_tmux_session",
                "agent_id": agent_id, "tmux_session": sess}

    # 选择 /clear 或 /compact: 距上次清理时间窗口 (任务级, 非字节)
    # 首次清理 (last_ts==0): 走 /clear, 没有 prior history 不该 /compact
    auto_compact_after = int(thr.get("auto_compact_after_seconds", 7200))
    parent_did = (mini_task or {}).get("parent_dispatch_id", "")

    if last_ts <= 0:
        return {"action": "clear",
                "reason": "first_cleanup_after_bus_task",
                "agent_id": agent_id, "tmux_session": sess,
                "elapsed_sec": -1,
                "parent_dispatch_id": parent_did}

    elapsed = time.time() - last_ts
    if elapsed >= auto_compact_after:
        return {"action": "compact",
                "reason": "elapsed_above_auto_compact_threshold",
                "agent_id": agent_id, "tmux_session": sess,
                "elapsed_sec": int(elapsed),
                "threshold_sec": auto_compact_after,
                "parent_dispatch_id": parent_did}
    return {"action": "clear",
            "reason": "bus_task_completed",
            "agent_id": agent_id, "tmux_session": sess,
            "elapsed_sec": int(elapsed),
            "parent_dispatch_id": parent_did}


def _audit(agent_id: str, action: str, result: str,
           initiated_by: str = "?", error: str = "",
           reason: str = "", parent_dispatch_id: str = "",
           elapsed_sec: int = -1):
    """pre_log/runtime/conversation_ops_YYYYMMDD.jsonl chmod 600 per-day rotation."""
    try:
        _RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_RUNTIME_LOG_DIR), 0o700)
        except OSError:
            pass
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = _RUNTIME_LOG_DIR / f"conversation_ops_{date_str}.jsonl"
        new_file = not log_file.exists()
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "action": action,
            "initiated_by": initiated_by,
            "result": result,
            "reason": reason,
            "parent_dispatch_id": parent_dispatch_id,
            "elapsed_sec": elapsed_sec,
            "error": error[:200] if error else "",
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if new_file:
            try:
                os.chmod(str(log_file), 0o600)
            except OSError:
                pass
    except OSError:
        pass


def _send_slash_command(tmux_session: str, slash: str) -> tuple[bool, str]:
    if not tmux_session or not slash:
        return False, "missing_args"
    if not has_session(tmux_session, timeout=2.0):
        return False, "tmux_session_dead"
    try:
        ok = send_to_tmux(tmux_session, slash, timeout=8.0, max_retry=1)
        if not ok:
            return False, "send_to_tmux_failed"
        return True, ""
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def _do_action(agent_id: str, action: str, initiated_by: str,
               force: bool, parent_dispatch_id: str = "",
               agent_meta_override: Optional[dict] = None) -> dict:
    """统一执行路径: clear / compact 共用."""
    if action not in ("clear", "compact"):
        return {"ok": False, "error": f"invalid_action:{action}", "agent_id": agent_id}
    if is_excluded(agent_id):
        _audit(agent_id, action, "blocked_excluded", initiated_by=initiated_by,
               reason="excluded", parent_dispatch_id=parent_dispatch_id)
        return {"ok": False, "error": "excluded", "agent_id": agent_id}
    cfg = load_config()
    if not cfg.get("enabled") and not force:
        _audit(agent_id, action, "blocked_disabled", initiated_by=initiated_by,
               reason="disabled", parent_dispatch_id=parent_dispatch_id)
        return {"ok": False, "error": "disabled", "agent_id": agent_id}
    state = _read_state(agent_id)
    last_ts = state.get("last_action_ts", 0.0)
    cd = (cfg.get("thresholds") or {}).get("cooldown_seconds", 30)
    if (time.time() - last_ts) < cd and not force:
        _audit(agent_id, action, "blocked_cooldown", initiated_by=initiated_by,
               reason="in_cooldown", parent_dispatch_id=parent_dispatch_id)
        return {"ok": False, "error": "in_cooldown", "agent_id": agent_id,
                "remaining_sec": int(cd - (time.time() - last_ts))}
    sess = _resolve_tmux_session(agent_id, agent_meta_override)
    if not sess:
        _audit(agent_id, action, "missing_tmux", initiated_by=initiated_by,
               parent_dispatch_id=parent_dispatch_id)
        return {"ok": False, "error": "missing_tmux_session", "agent_id": agent_id}
    slash = f"/{action}"
    ok, err = _send_slash_command(sess, slash)
    now = time.time()
    new_state = {
        "last_action_ts": now,
        "last_action": action,
        "tasks_since_cleanup": 0,  # reset
        "last_dispatch_id": parent_dispatch_id,
    }
    _write_state(agent_id, new_state)
    elapsed = int(now - last_ts) if last_ts > 0 else -1
    _audit(agent_id, action, "ok" if ok else "failed",
           initiated_by=initiated_by, error=err,
           parent_dispatch_id=parent_dispatch_id, elapsed_sec=elapsed)
    return {"ok": ok, "action": action, "agent_id": agent_id,
            "tmux_session": sess, "error": err if not ok else ""}


def clear(agent_id: str, initiated_by: str = "?", force: bool = False,
          parent_dispatch_id: str = "",
          agent_meta_override: Optional[dict] = None) -> dict:
    return _do_action(agent_id, "clear", initiated_by, force, parent_dispatch_id,
                      agent_meta_override=agent_meta_override)


def compact(agent_id: str, initiated_by: str = "?", force: bool = False,
            parent_dispatch_id: str = "",
            agent_meta_override: Optional[dict] = None) -> dict:
    return _do_action(agent_id, "compact", initiated_by, force, parent_dispatch_id,
                      agent_meta_override=agent_meta_override)


def auto_evaluate(agent_id: str, mini_task: Optional[dict] = None,
                   initiated_by: str = "stop_analyzer",
                   agent_meta_override: Optional[dict] = None) -> dict:
    """事件入口: stop_analyzer 推完 mini_task 后调.
    决策 + (如需) 执行. 任何 noop / fail-safe 都不抛异常.
    返 should_act 结果, 加 executed=True/False.

     phase 3: 硬门槛 (should_act 返 noop) 优先, LLM 不可 override.
    硬门槛过后, 若 llm_evaluator.enabled → 调 conversation_evaluator.evaluate, LLM 主导决策;
    LLM 失败/不可达 → fallback 规则结果 (phase 2 first/elapsed/clear).
    """
    decision = should_act(agent_id, mini_task=mini_task,
                           agent_meta_override=agent_meta_override)
    decision["executed"] = False
    parent_did = (mini_task or {}).get("parent_dispatch_id", "")

    # 硬门槛 noop (disabled/excluded/no_pdi/cooldown/state_not_idle/missing_tmux):
    # LLM 不可 override. 直接返 + 增计数 + audit.
    if decision.get("action") == "noop":
        try:
            state = _read_state(agent_id)
            if _has_parent_dispatch(mini_task):
                state["tasks_since_cleanup"] = int(state.get("tasks_since_cleanup", 0)) + 1
                state["last_dispatch_id"] = parent_did or state.get("last_dispatch_id", "")
                _write_state(agent_id, state)
        except (OSError, ValueError):
            pass
        _audit(agent_id, "evaluate", "noop", initiated_by=initiated_by,
               reason=decision.get("reason", ""),
               parent_dispatch_id=parent_did)
        return decision

    # 硬门槛已过 (action ∈ clear/compact). 尝试 LLM evaluator 主导决策.
    rule_action = decision.get("action")
    rule_reason = decision.get("reason")
    decision["rule_action"] = rule_action
    decision["rule_reason"] = rule_reason

    llm_result = None
    try:
        cfg = load_config()
        llm_cfg = cfg.get("llm_evaluator") or {}
        if llm_cfg.get("enabled"):
            state = _read_state(agent_id)
            last_ts = state.get("last_action_ts", 0.0)
            elapsed_since = int(time.time() - last_ts) if last_ts > 0 else -1
            ctx = {
                "last_action": state.get("last_action", ""),
                "elapsed_since_last_cleanup_sec": elapsed_since,
            }
            from src.runtime.conversation_evaluator import evaluate as _llm_eval  # type: ignore
            llm_result = _llm_eval(agent_id, mini_task=mini_task, ctx=ctx)
    except ImportError:
        llm_result = None
    except (OSError, ValueError, RuntimeError):
        llm_result = None
    except Exception:  # noqa: BLE001 — fail-safe, 任何异常 fallback 规则
        llm_result = None

    if llm_result and llm_result.get("action") in ("clear", "compact", "noop"):
        decision["llm"] = {
            "action": llm_result.get("action"),
            "reason": llm_result.get("reason", ""),
            "confidence": llm_result.get("confidence", 0.0),
            "elapsed_ms": llm_result.get("elapsed_ms", 0),
            "provider": llm_result.get("provider", "gemini"),
        }
        decision["action"] = llm_result["action"]
        decision["reason"] = f"llm:{llm_result.get('reason', '')}"

    # LLM 推 noop (推迟动作): 仅记 audit, 不执行 slash. 增 task counter.
    if decision.get("action") == "noop":
        try:
            state = _read_state(agent_id)
            if _has_parent_dispatch(mini_task):
                state["tasks_since_cleanup"] = int(state.get("tasks_since_cleanup", 0)) + 1
                state["last_dispatch_id"] = parent_did or state.get("last_dispatch_id", "")
                _write_state(agent_id, state)
        except (OSError, ValueError):
            pass
        _audit(agent_id, "evaluate", "noop_llm_override", initiated_by=initiated_by,
               reason=decision.get("reason", ""),
               parent_dispatch_id=parent_did)
        return decision

    if decision.get("action") == "clear":
        r = clear(agent_id, initiated_by=initiated_by, parent_dispatch_id=parent_did,
                  agent_meta_override=agent_meta_override)
        decision["executed"] = bool(r.get("ok"))
        decision["execution_result"] = r
    elif decision.get("action") == "compact":
        r = compact(agent_id, initiated_by=initiated_by, parent_dispatch_id=parent_did,
                    agent_meta_override=agent_meta_override)
        decision["executed"] = bool(r.get("ok"))
        decision["execution_result"] = r
    return decision
