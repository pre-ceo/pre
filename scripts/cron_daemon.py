#!/usr/bin/env python3
"""
pre/scripts/cron_daemon.py — 平台 cron 调度独立进程 (B 方案 v2)

[已 deprecated ]: master 内嵌 cron 已替代 (src/master/cron.py).
  master 启动时自动跑 cron loop, 跨 node 走 ws RPC 不再 ssh subprocess 拐弯.
  本文件代码保留作历史 (按宪法), bus_ctl.sh start cron 仍可启但 deprecated warning.

设计:
- stdlib only (asyncio + fcntl + http.server + subprocess + zoneinfo)
- 独立进程, 0 master 改动
- daily / weekly / interval 三类型 (cron expr 留 Phase B)
- fcntl.flock 防双 fire
- 失败重试 3 次退避 30s/2min/5min, ≥5 连续失败 auto_disable + finding
- monotonic 跳变检测 (机器睡眠 / NTP)
- hot reload schedules.json 每 tick
- 触发: subprocess.Popen detached (HC-PRE-cron-1)
- 每触发写 master message kind=cron_trigger (走 /api/v1/cron/trigger)
- healthz: 内置 HTTP server 19501

数据:
- pre_rule/cron/schedules.json (config, 用户编辑)
- pre_rule/cron/state.json (runtime, daemon 写)
- pre_rule/cron/daemon.lock (fcntl)
- pre_log/cron/cron_YYYYMMDD.jsonl (触发日志)

启动:
    uv run python scripts/cron_daemon.py
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

# Loopback master call: direct, bypass proxy env (Surge etc.)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # py < 3.9 fallback (实际 pre 要求 3.11+)


# ---------- 路径常量 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULE_ROOT = (PROJECT_ROOT.parent / "pre_rule").resolve()
LOG_ROOT = (PROJECT_ROOT.parent / "pre_log").resolve()

CRON_DIR = RULE_ROOT / "cron"
CRON_LOG_DIR = LOG_ROOT / "cron"
SCHEDULES_FILE = CRON_DIR / "schedules.json"
STATE_FILE = CRON_DIR / "state.json"
LOCK_FILE = CRON_DIR / "daemon.lock"

# ---------- 配置常量 ----------
TICK_INTERVAL_SEC = 30           # main loop sleep
HEALTHZ_PORT = 19501             # GET / 端点 (master 19500 +1)
DEFAULT_MAX_FAILURES = 5
RETRY_BACKOFF_SEC = (30, 120, 300)  # 30s, 2min, 5min
JUMP_THRESHOLD_SEC = 60          # monotonic vs wall 跳变阈值
MASTER_URL = os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500")
MASTER_TOKEN = os.environ.get("PRE_SECRET", "pre")

# ---------- daemon 全局状态 (healthz 用, 主线程 + http server thread 共享) ----------
DAEMON_STATE = {
    "started_ts": time.time(),
    "tick_count": 0,
    "last_tick_ts": None,
    "schedules_loaded": 0,
    "schedules_disabled": 0,
    "pid": os.getpid(),
}


# ---------- 日志 ----------

def _log_trigger(entry: dict):
    """写 pre_log/cron/cron_YYYYMMDD.jsonl, 每行一个 JSON event."""
    try:
        CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = CRON_LOG_DIR / f"cron_{date_str}.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # fail-safe (HC-PRE-2)


def _log_event(level: str, schedule_id: str, msg: str, **extra):
    """daemon 自身事件 (启动/异常/auto_disable 等), stdout + jsonl."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "schedule_id": schedule_id,
        "msg": msg,
        **extra,
    }
    try:
        print(f"[cron] {level} {schedule_id}: {msg}", flush=True)
    except OSError:
        pass
    _log_trigger({**rec, "type": "daemon_event"})


# ---------- 文件 I/O (atomic write) ----------

def _load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json_atomic(path: Path, data):
    """write to .tmp + rename (atomic on POSIX)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(str(tmp), str(path))
    except OSError as e:
        _log_event("error", "_state_io", f"save {path.name} failed: {e}")


# ---------- 锁 (fcntl.flock 防双 daemon, / HC-A8) ----------

def acquire_lock_or_exit() -> int:
    """try acquire LOCK_EX | LOCK_NB; 失败立即 exit."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        # 尝试读已有 lock holder pid
        try:
            with open(LOCK_FILE) as f:
                holder_pid = f.read().strip()
        except OSError:
            holder_pid = "?"
        print(f"[cron] another daemon already holds {LOCK_FILE} (pid={holder_pid}), exiting.",
              file=sys.stderr, flush=True)
        sys.exit(1)
    # write our pid
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)
    return fd


# ---------- 时间计算 ----------

def _now_dt(tz_name: str | None) -> datetime:
    if tz_name and ZoneInfo:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def compute_next_run_daily(time_str: str, tz_name: str | None,
                           now_dt: datetime) -> float:
    """daily: 下一个 HH:MM (>= now)."""
    h, m = _parse_hhmm(time_str)
    target = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now_dt:
        target += timedelta(days=1)
    return target.timestamp()


def compute_next_run_weekly(weekday: int, time_str: str, tz_name: str | None,
                            now_dt: datetime) -> float:
    """weekly: 下一个 weekday HH:MM (>= now). weekday: 0=Monday."""
    h, m = _parse_hhmm(time_str)
    cur_wd = now_dt.weekday()
    days_ahead = (weekday - cur_wd) % 7
    target = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    target += timedelta(days=days_ahead)
    if target <= now_dt:
        target += timedelta(days=7)
    return target.timestamp()


def compute_next_run_interval(every_seconds: int, last_run_ts: float | None,
                              now_ts: float) -> float:
    """interval: last_run + every_seconds (或 now+every 如果从未跑过)."""
    if not last_run_ts:
        return now_ts + every_seconds
    return last_run_ts + every_seconds


def compute_next_run(sched: dict, last_run_ts: float | None,
                     now_ts: float) -> float | None:
    """根据 schedule type 算 next_run_ts. 返 None 表示 schedule 配置错误."""
    stype = sched.get("type")
    tz_name = sched.get("tz")
    now_dt = _now_dt(tz_name)
    try:
        if stype == "daily":
            return compute_next_run_daily(sched["time"], tz_name, now_dt)
        elif stype == "weekly":
            return compute_next_run_weekly(int(sched["weekday"]), sched["time"],
                                           tz_name, now_dt)
        elif stype == "interval":
            every = int(sched["every_seconds"])
            if every < 30:
                _log_event("warn", sched.get("id", "?"),
                           f"interval every_seconds={every} too small, min 30")
                return None
            return compute_next_run_interval(every, last_run_ts, now_ts)
        else:
            _log_event("warn", sched.get("id", "?"), f"unknown type: {stype}")
            return None
    except (KeyError, ValueError) as e:
        _log_event("error", sched.get("id", "?"), f"compute_next_run failed: {e}")
        return None


# ---------- master cron_trigger audit (best-effort) ----------

def _post_master_cron_trigger(payload: dict):
    """POST /api/v1/cron/trigger, fail-safe. 不重试 (cron audit 是 best-effort)."""
    body = json.dumps({
        "from_agent": "cron.daemon",
        "to_agent": "audit.cron",
        "from_role": "platform",
        "payload": payload,
    }).encode("utf-8")
    req = urllib.request.Request(
        MASTER_URL.rstrip("/") + "/api/v1/cron/trigger",
        data=body,
        headers={"Authorization": f"Bearer {MASTER_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _NO_PROXY_OPENER.open(req, timeout=5) as r:
            r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        pass  # silent skip (HC-PRE-2)


# ---------- finding (auto_disable 通知) ----------

def _write_finding(schedule_id: str, reason: str):
    """写 pre/pre/findings/WARNING-cron-{id}-disabled.md (stop hook 处理)."""
    findings_dir = PROJECT_ROOT / "pre" / "findings"
    try:
        findings_dir.mkdir(parents=True, exist_ok=True)
        # title 用安全字符
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in schedule_id)[:60]
        fpath = findings_dir / f"WARNING-cron-{safe_id}-disabled.md"
        content = (
            f"# WARNING: cron schedule {schedule_id} auto-disabled\n\n"
            f"## Trigger\n\n"
            f"consecutive_failures ≥ {DEFAULT_MAX_FAILURES} → auto_disabled.\n\n"
            f"## Last error\n\n"
            f"```\n{reason}\n```\n\n"
            f"## Action\n\n"
            f"1. 排查 cmd 失败原因 (查 pre_log/cron/cron_*.jsonl)\n"
            f"2. 修问题后, 编辑 {SCHEDULES_FILE} 把 enabled=true\n"
            f"3. state.json 中 auto_disabled 字段重置 false (或重启 daemon 自动清零)\n"
        )
        fpath.write_text(content, encoding="utf-8")
    except OSError:
        pass


# ---------- 单 schedule 执行 (detached) ----------

async def _run_one_safely(sched: dict, state: dict, attempt: int = 1):
    """spawn detached subprocess 跑 cmd, 等结果, 更新 state, 失败重试.
    HC-PRE-cron-1: subprocess.Popen + start_new_session = detach.
    HC-PRE-2: 任何异常 silent skip (不冒泡 main loop).
    M-A3: 重试 3 次退避; payload 异常立即 failed (不 retry).
    """
    sid = sched["id"]
    cmd = sched.get("cmd")
    cwd = sched.get("cwd") or None
    env_extra = sched.get("env") or {}

    # payload 异常 (无 cmd / cmd 不是 list) → 立即 failed, 不 retry
    if not cmd or not isinstance(cmd, list):
        _log_event("error", sid, "invalid cmd (must be list)", attempt=attempt)
        _update_state_after_run(state, sid, status="failed", returncode=-1,
                                error="invalid cmd payload",
                                duration_ms=0, increment_failure=True)
        _post_master_cron_trigger({
            "schedule_id": sid, "trigger_ts": time.time(),
            "cmd": cmd, "returncode": -1, "duration_ms": 0,
            "attempt": attempt, "result": "failed_final_invalid_payload",
        })
        return

    env = os.environ.copy()
    if isinstance(env_extra, dict):
        env.update({str(k): str(v) for k, v in env_extra.items()})

    started = time.time()
    started_mono = time.monotonic()
    returncode = -1
    error = ""

    try:
        # detached: start_new_session 让子进程独立 process group
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        try:
            _, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=600.0,  # 10min 上限
            )
            returncode = proc.returncode if proc.returncode is not None else -1
            if returncode != 0 and stderr_data:
                error = stderr_data[:1000].decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            returncode = -2
            error = "timeout > 600s"
    except Exception as e:
        returncode = -3
        error = f"{type(e).__name__}: {str(e)[:300]}"
        _log_event("error", sid, f"subprocess spawn failed: {error}",
                   attempt=attempt)

    duration_ms = int((time.monotonic() - started_mono) * 1000)
    success = (returncode == 0)

    # log + state
    _log_trigger({
        "ts": datetime.now(timezone.utc).isoformat(),
        "schedule_id": sid,
        "type": sched.get("type"),
        "cmd": cmd,
        "returncode": returncode,
        "duration_ms": duration_ms,
        "attempt": attempt,
        "status": "ok" if success else "failed",
        "error": error[:500] if error else "",
    })

    if success:
        _update_state_after_run(state, sid, status="ok", returncode=0,
                                error="", duration_ms=duration_ms,
                                increment_failure=False)
        _post_master_cron_trigger({
            "schedule_id": sid, "trigger_ts": started,
            "cmd": cmd, "returncode": 0, "duration_ms": duration_ms,
            "attempt": attempt, "result": "ok",
        })
        return

    # 失败 → 看是否 retry
    if attempt < len(RETRY_BACKOFF_SEC) + 1:  # 重试 (RETRY_BACKOFF_SEC 长度) 次
        backoff = RETRY_BACKOFF_SEC[attempt - 1]
        _log_event("warn", sid,
                   f"failed (rc={returncode}, attempt={attempt}), retry in {backoff}s",
                   error=error[:200])
        _update_state_after_run(state, sid, status="retrying",
                                returncode=returncode, error=error[:500],
                                duration_ms=duration_ms,
                                increment_failure=False)
        _post_master_cron_trigger({
            "schedule_id": sid, "trigger_ts": started,
            "cmd": cmd, "returncode": returncode, "duration_ms": duration_ms,
            "attempt": attempt, "result": "failed_will_retry",
        })
        # save state 后 schedule 重试
        _save_json_atomic(STATE_FILE, state)
        await asyncio.sleep(backoff)
        await _run_one_safely(sched, state, attempt=attempt + 1)
        return

    # 重试用完 → 最终 failed
    _update_state_after_run(state, sid, status="failed", returncode=returncode,
                            error=error[:500], duration_ms=duration_ms,
                            increment_failure=True)

    sst = state["schedules"].get(sid, {})
    cnt = sst.get("consecutive_failures", 0)
    max_fail = int(sched.get("max_consecutive_failures", DEFAULT_MAX_FAILURES))
    auto_disabled = cnt >= max_fail
    if auto_disabled:
        sst["auto_disabled"] = True
        sst["auto_disabled_reason"] = (
            f"consecutive_failures={cnt} >= {max_fail}, last_error={error[:200]}"
        )
        state["schedules"][sid] = sst
        _write_finding(sid, sst["auto_disabled_reason"])
        _log_event("warn", sid, "auto_disabled after consecutive failures",
                   consecutive_failures=cnt)

    _post_master_cron_trigger({
        "schedule_id": sid, "trigger_ts": started,
        "cmd": cmd, "returncode": returncode, "duration_ms": duration_ms,
        "attempt": attempt,
        "result": "auto_disabled" if auto_disabled else "failed_final",
    })


def _update_state_after_run(state: dict, sid: str, status: str, returncode: int,
                            error: str, duration_ms: int,
                            increment_failure: bool):
    state.setdefault("schedules", {})
    sst = state["schedules"].get(sid, {})
    sst["last_run_ts"] = time.time()
    sst["last_run_status"] = status
    sst["last_run_returncode"] = returncode
    sst["last_run_error"] = error
    sst["last_run_duration_ms"] = duration_ms
    if status == "ok":
        sst["consecutive_failures"] = 0
    elif increment_failure:
        sst["consecutive_failures"] = sst.get("consecutive_failures", 0) + 1
    state["schedules"][sid] = sst


# ---------- main loop ----------

async def cron_loop():
    """每 30s 一 tick: 读 schedules.json + state.json, 找 due jobs, 触发."""
    last_mono = time.monotonic()
    last_wall = time.time()

    while True:
        try:
            DAEMON_STATE["tick_count"] += 1
            DAEMON_STATE["last_tick_ts"] = time.time()

            # monotonic 跳变检测 (机器睡眠 / NTP, )
            cur_mono = time.monotonic()
            cur_wall = time.time()
            mono_dt = cur_mono - last_mono
            wall_dt = cur_wall - last_wall
            jump = abs(wall_dt - mono_dt)
            if jump > JUMP_THRESHOLD_SEC:
                _log_event("warn", "_loop",
                           f"clock jump detected: wall_dt={wall_dt:.1f}s mono_dt={mono_dt:.1f}s, "
                           f"recomputing all next_run")
                # 跳变时强制重算所有 next_run (compute_next_run 用 last_run + every 或 now)
            last_mono = cur_mono
            last_wall = cur_wall

            # hot reload schedules.json (HC-PRE-cron-2)
            sched_doc = _load_json(SCHEDULES_FILE, {"version": 1, "schedules": []})
            schedules = sched_doc.get("schedules", []) or []

            # state.json
            state = _load_json(STATE_FILE, {"schedules": {}})
            state.setdefault("schedules", {})
            state["daemon_heartbeat_ts"] = time.time()
            state["daemon_pid"] = os.getpid()
            state.setdefault("daemon_started_ts", DAEMON_STATE["started_ts"])

            DAEMON_STATE["schedules_loaded"] = len(schedules)
            DAEMON_STATE["schedules_disabled"] = sum(
                1 for s in schedules
                if not s.get("enabled", True)
                or state["schedules"].get(s.get("id", ""), {}).get("auto_disabled")
            )

            # 找 due jobs
            now_ts = time.time()
            due = []
            for sched in schedules:
                sid = sched.get("id")
                if not sid:
                    continue
                if not sched.get("enabled", True):
                    continue
                sst = state["schedules"].setdefault(sid, {})
                if sst.get("auto_disabled"):
                    continue
                if jump > JUMP_THRESHOLD_SEC:
                    # 跳变后重算 next_run
                    nrt = compute_next_run(sched, sst.get("last_run_ts"), now_ts)
                    if nrt is not None:
                        sst["next_run_ts"] = nrt
                next_run_ts = sst.get("next_run_ts")
                if next_run_ts is None:
                    nrt = compute_next_run(sched, sst.get("last_run_ts"), now_ts)
                    if nrt is None:
                        continue
                    sst["next_run_ts"] = nrt
                    next_run_ts = nrt
                if next_run_ts <= now_ts:
                    due.append(sched)
                    # 提前推下一次 next_run, 防 re-fire (即使本次 _run_one_safely 还没跑完)
                    new_next = compute_next_run(sched, now_ts, now_ts)
                    if new_next:
                        sst["next_run_ts"] = new_next

            _save_json_atomic(STATE_FILE, state)

            # 并发触发 due jobs (每个独立 task, 失败 isolate)
            for sched in due:
                _log_event("info", sched["id"], "trigger",
                           type=sched.get("type"))
                # asyncio.create_task 让 _run_one_safely 异步跑, 不阻塞 main loop
                asyncio.create_task(_run_one_safely(sched, state))

        except Exception as e:
            _log_event("error", "_loop",
                       f"tick failed: {type(e).__name__}: {e}",
                       traceback=traceback.format_exc()[:1000])

        await asyncio.sleep(TICK_INTERVAL_SEC)


# ---------- healthz HTTP server (sync, 跑 thread) ----------

class HealthzHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/" and self.path != "/healthz":
            self.send_error(404)
            return
        body = json.dumps({
            "ok": True,
            "daemon_pid": DAEMON_STATE["pid"],
            "started_ts": DAEMON_STATE["started_ts"],
            "uptime_sec": int(time.time() - DAEMON_STATE["started_ts"]),
            "tick_count": DAEMON_STATE["tick_count"],
            "last_tick_ts": DAEMON_STATE["last_tick_ts"],
            "schedules_loaded": DAEMON_STATE["schedules_loaded"],
            "schedules_disabled": DAEMON_STATE["schedules_disabled"],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # silence default access log


def start_healthz_server():
    """跑 thread daemon 的 stdlib HTTPServer."""
    try:
        server = HTTPServer(("127.0.0.1", HEALTHZ_PORT), HealthzHandler)
        t = Thread(target=server.serve_forever, daemon=True, name="cron-healthz")
        t.start()
        _log_event("info", "_healthz", f"listening 127.0.0.1:{HEALTHZ_PORT}")
    except OSError as e:
        _log_event("warn", "_healthz", f"failed to start: {e}")


# ---------- 入口 ----------

def main():
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 防双 fire ()
    lock_fd = acquire_lock_or_exit()
    _log_event("info", "_daemon",
               f"started, pid={os.getpid()}, schedules_file={SCHEDULES_FILE}")

    # 信号处理: 优雅退出
    def _shutdown(signum, frame):
        _log_event("info", "_daemon", f"signal {signum} → shutdown")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # healthz
    start_healthz_server()

    # main asyncio loop
    try:
        asyncio.run(cron_loop())
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
