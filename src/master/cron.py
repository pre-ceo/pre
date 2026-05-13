"""
pre/src/master/cron.py — master 内嵌 cron loop.

跟 cron_daemon.py 主体逻辑一致, 但跑在 master event loop:
- 30s tick, 读 pre_rule/cron/schedules.json
- 找 due jobs:
  - target_node=local → master 直接 subprocess.Popen detached
  - target_node=<node_id> → ws RPC "exec_cmd" 推 node, node 跑

stdlib only. fail-safe: 任何 schedule fail silent skip.
audit log pre_log/cron/cron_YYYYMMDD.jsonl (跟 同模式).
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from ws_lib import send_to_writer

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RULE_ROOT = (_PROJECT_ROOT.parent / "pre_rule").resolve()
LOG_ROOT = (_PROJECT_ROOT.parent / "pre_log").resolve()
CRON_DIR = RULE_ROOT / "cron"
CRON_LOG_DIR = LOG_ROOT / "cron"
SCHEDULES_FILE = CRON_DIR / "schedules.json"
STATE_FILE = CRON_DIR / "state.json"

TICK_INTERVAL = 30.0
DEFAULT_MAX_FAILURES = 5
RETRY_BACKOFF = (30, 120, 300)


def _log_trigger(entry: dict):
    try:
        CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        # M1 spec A: audit jsonl 全集 redact
        try:
            from master.redact import safe_audit_dump as _safe_dump
            _line = _safe_dump(entry)
        except ImportError:
            _line = json.dumps(entry, ensure_ascii=False)
        with open(CRON_LOG_DIR / f"cron_{date_str}.jsonl", "a", encoding="utf-8") as f:
            f.write(_line + "\n")
    except OSError:
        pass


def _load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_state(state: dict):
    try:
        CRON_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(str(tmp), str(STATE_FILE))
    except OSError:
        pass


def _now_dt(tz_name: Optional[str]) -> datetime:
    if tz_name and ZoneInfo:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def _next_run(sched: dict, last_run_ts: Optional[float], now_ts: float) -> Optional[float]:
    stype = sched.get("type")
    tz_name = sched.get("tz")
    now_dt = _now_dt(tz_name)
    try:
        if stype == "daily":
            h, m = _parse_hhmm(sched["time"])
            t = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if t <= now_dt:
                t += timedelta(days=1)
            return t.timestamp()
        if stype == "weekly":
            h, m = _parse_hhmm(sched["time"])
            wd = int(sched["weekday"])
            cur = now_dt.weekday()
            ahead = (wd - cur) % 7
            t = now_dt.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=ahead)
            if t <= now_dt:
                t += timedelta(days=7)
            return t.timestamp()
        if stype == "interval":
            every = int(sched["every_seconds"])
            if every < 30:
                return None
            # 首次没 last_run → 立即 due (now), 不等 every (避免 master 重启
            # 后 10min 才第一次跑); 有 last_run → last + every
            if last_run_ts:
                return last_run_ts + every
            return now_ts
    except (KeyError, ValueError):
        return None
    return None


async def _run_local(sched: dict, state: dict):
    """target_node=local: master 端 subprocess.Popen detached"""
    sid = sched["id"]
    cmd = sched.get("cmd") or []
    cwd = sched.get("cwd") or None
    env_extra = sched.get("env") or {}
    if not isinstance(cmd, list) or not cmd:
        _log_trigger({"ts": time.time(), "schedule_id": sid, "decision": "invalid_cmd"})
        return
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in env_extra.items()})
    started = time.time()
    try:
        # detached, fire-and-forget
        await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd, env=env, start_new_session=True,
        )
        _log_trigger({"ts": started, "schedule_id": sid, "target": "local",
                      "cmd": cmd, "status": "spawned"})
    except Exception as e:
        _log_trigger({"ts": started, "schedule_id": sid, "target": "local",
                      "cmd": cmd, "status": "spawn_failed", "error": str(e)[:200]})


async def _run_remote(sched: dict, state: dict, registry):
    """target_node=<node_id>: ws RPC exec_cmd 推 node"""
    sid = sched["id"]
    target = sched.get("target_node")
    cmd = sched.get("cmd") or []
    cwd = sched.get("cwd") or ""
    env_extra = sched.get("env") or {}
    node = registry.get_node(target)
    if not node or not node.ws_writer:
        _log_trigger({"ts": time.time(), "schedule_id": sid, "target": target,
                      "status": "node_offline"})
        return
    rpc = {
        "jsonrpc": "2.0",
        "method": "exec_cmd",
        "params": {
            "cmd": cmd, "cwd": cwd, "env": env_extra, "schedule_id": sid,
        },
    }
    try:
        await send_to_writer(node.ws_writer, json.dumps(rpc, ensure_ascii=False))
        _log_trigger({"ts": time.time(), "schedule_id": sid, "target": target,
                      "cmd": cmd, "status": "rpc_sent"})
    except Exception as e:
        _log_trigger({"ts": time.time(), "schedule_id": sid, "target": target,
                      "cmd": cmd, "status": "rpc_failed", "error": str(e)[:200]})


async def cron_loop(registry, db):
    """master 内嵌 cron loop, 30s tick. 入口由 run_master 调."""
    print(f"[master-cron] starting (schedules={SCHEDULES_FILE})", flush=True)
    while True:
        try:
            doc = _load_json(SCHEDULES_FILE, {"version": 1, "schedules": []})
            schedules = doc.get("schedules") or []
            state = _load_json(STATE_FILE, {"schedules": {}})
            state.setdefault("schedules", {})
            state["heartbeat_ts"] = time.time()

            now_ts = time.time()
            for sched in schedules:
                sid = sched.get("id")
                if not sid or not sched.get("enabled", True):
                    continue
                sst = state["schedules"].setdefault(sid, {})
                if sst.get("auto_disabled"):
                    continue
                next_run_ts = sst.get("next_run_ts")
                if next_run_ts is None:
                    nrt = _next_run(sched, sst.get("last_run_ts"), now_ts)
                    if nrt is None:
                        continue
                    sst["next_run_ts"] = nrt
                    next_run_ts = nrt
                if next_run_ts > now_ts:
                    continue
                # due → 触发
                target = sched.get("target_node") or "local"
                # 远端 node 还没 connect 时不 mark, 让下一轮 (30s) 重试
                if target != "local":
                    n = registry.get_node(target)
                    if not n or not n.ws_writer:
                        # node 还没注册, 推迟 30s 重试 (next_run_ts = now + 30, < 600s 间隔)
                        sst["next_run_ts"] = now_ts + 30
                        continue
                sst["last_run_ts"] = now_ts
                new_next = _next_run(sched, now_ts, now_ts)
                if new_next:
                    sst["next_run_ts"] = new_next
                # 异步触发, 不阻塞 main loop
                if target == "local":
                    asyncio.create_task(_run_local(sched, state))
                else:
                    asyncio.create_task(_run_remote(sched, state, registry))

            _save_state(state)
        except Exception as e:
            _log_trigger({"ts": time.time(), "schedule_id": "_loop",
                          "status": "error", "error": str(e)[:200],
                          "tb": traceback.format_exc()[:500]})
        await asyncio.sleep(TICK_INTERVAL)
