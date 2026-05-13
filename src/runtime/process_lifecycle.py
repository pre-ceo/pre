"""
process_lifecycle — fn_runtime phase 1.
import urllib.request
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

进程 + tmux 生命周期管理. 仅 fn_* infrastructure scope (业务 agent PM2 自管, ).

API:
  load_config() -> dict (mtime hot reload)
  list_targets() -> list[str]
  health(target_id) -> dict {alive, tmux_session_alive, pid_alive, port_listening, pid?}
  start(target_id, force=False) -> dict
  stop(target_id, force=False) -> dict
  restart(target_id) -> dict
  scan_crashes() -> list[dict] # cron 兜底入口, 检查所有 enabled target, 0 LLM cost

复用: tmux_helper / ssh_sudo_allowlist / notify_abstract / mtime hot reload.
HC-PRE-1 stdlib only. HC-A9/G10 polling 禁止 (单次 syscall, 不 sleep+repeat).
"""
from __future__ import annotations
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from common.paths import PRE_RULE_ROOT, PRE_LOG_ROOT, PRE_AGENT_HOME
from typing import Optional

# token: lazy resolve from ~/.pre/env via token_resolver (PR3)
try:
    from src.common.token_resolver import resolve as _resolve_token  # hook context
except ImportError:
    from common.token_resolver import resolve as _resolve_token  # master context

# 复用 tmux_helper
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from tmux_helper import has_session, capture_pane, find_tmux
except ImportError:
    def has_session(s, timeout=3.0): return False
    def capture_pane(s, lines=10, timeout=3.0): return ""
    def find_tmux(): return shutil.which("tmux") or "tmux"


# 路径常量
_RULE_PATH = Path(os.environ.get(
    "PRE_RUNTIME_PROCESSES",
    str(Path(PRE_RULE_ROOT) / "runtime" / "processes.json"),
))
_LOG_DIR = Path(os.environ.get(
    "PRE_LOG_DIR",
    PRE_LOG_ROOT,
))
_RUNTIME_LOG_DIR = _LOG_DIR / "runtime"
_FINDINGS_HOME = Path(PRE_AGENT_HOME)  # findings 写到 {project}/pre/findings/


def _resolve_tmux_rc() -> str:
    """— locate tmux_startup.sh rc file. Order:
    1. $PRE_RULE_ROOT/tmux_startup.sh
    2. <PRE_RULE_ROOT>/tmux_startup.sh
    3. <pre>/scripts/tmux_startup.sh (project fallback)
    Returns empty string if none found (caller should skip wrap)."""
    rule_root = PRE_RULE_ROOT
    candidates = [
        Path(rule_root) / "tmux_startup.sh",
        Path(PRE_RULE_ROOT) / "tmux_startup.sh",
        Path(__file__).parent.parent.parent / "scripts" / "tmux_startup.sh",
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return ""

# config cache (mtime hot reload)
_CACHE: dict = {"mtime": 0.0, "cfg": None}


def load_config() -> dict:
    """mtime hot reload. fail-safe: config 不可读 → empty dict (上层操作 noop)."""
    try:
        if not _RULE_PATH.exists():
            return {"version": 1, "targets": {}}
        mtime = _RULE_PATH.stat().st_mtime
        if _CACHE["cfg"] is not None and _CACHE["mtime"] == mtime:
            return _CACHE["cfg"]
        with open(_RULE_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        _CACHE["cfg"] = cfg
        _CACHE["mtime"] = mtime
        return cfg
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "targets": {}}


def list_targets(only_enabled: bool = False) -> list[str]:
    cfg = load_config()
    targets = cfg.get("targets") or {}
    if only_enabled:
        return [tid for tid, t in targets.items() if t.get("enabled")]
    return list(targets.keys())


def get_target(target_id: str) -> Optional[dict]:
    cfg = load_config()
    return (cfg.get("targets") or {}).get(target_id)


def _expand_path(p: str) -> str:
    return os.path.expanduser(p) if p else p


def _read_pid_file(pid_file_rel: str) -> Optional[int]:
    """读 pid file (相对 pre_log 路径). fail-safe → None."""
    if not pid_file_rel:
        return None
    if pid_file_rel.startswith("pre_log/"):
        full = _LOG_DIR / pid_file_rel[len("pre_log/"):]
    else:
        full = _expand_path(pid_file_rel)
        full = Path(full)
    try:
        if not full.exists():
            return None
        with open(full) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_pid_file(pid_file_rel: str, pid: int) -> Optional[Path]:
    """写 pid file. fail-safe → None."""
    if not pid_file_rel:
        return None
    if pid_file_rel.startswith("pre_log/"):
        full = _LOG_DIR / pid_file_rel[len("pre_log/"):]
    else:
        full = Path(_expand_path(pid_file_rel))
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        with open(full, "w") as f:
            f.write(str(pid))
        try:
            os.chmod(str(full), 0o600)
        except OSError:
            pass
        return full
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    """kill -0 syscall. fail-safe → False."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _port_listening(port: int) -> bool:
    """检查 127.0.0.1:port 是否有 listener. 0 LLM cost syscall."""
    if not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            r = s.connect_ex(("127.0.0.1", int(port)))
            return r == 0
    except (OSError, socket.timeout, ValueError):
        return False


def health(target_id: str) -> dict:
    """检查 target 健康状态. 0 LLM cost (全 syscall).
    返 {alive, tmux_session_alive, pid_alive, port_listening, pid, missing_target}.
    """
    target = get_target(target_id)
    if not target:
        return {"alive": False, "missing_target": True, "target_id": target_id}
    hc = target.get("health_check") or {}
    out = {
        "target_id": target_id,
        "tmux_session_alive": None,
        "pid_alive": None,
        "port_listening": None,
        "pid": None,
        "alive": False,
    }
    # tmux session 检查
    tmux_session = target.get("tmux_session") or ""
    if hc.get("tmux_session_alive") is not False and tmux_session:
        out["tmux_session_alive"] = has_session(tmux_session, timeout=2.0)
    # pid file 检查
    pid_file = target.get("pid_file") or ""
    if hc.get("pid_file_check") is not False and pid_file:
        pid = _read_pid_file(pid_file)
        out["pid"] = pid
        out["pid_alive"] = _pid_alive(pid) if pid else False
    # port 检查
    port = hc.get("port")
    if port:
        out["port_listening"] = _port_listening(port)
    # alive 综合判断 (tmux 优先, port 次之, pid 兜底)
    checks = [out["tmux_session_alive"], out["port_listening"], out["pid_alive"]]
    explicit = [c for c in checks if c is not None]
    out["alive"] = bool(explicit) and all(c for c in explicit)
    return out


def _audit(action: str, target_id: str, result: str,
           initiated_by: str = "?", error: str = "",
           pid_before: Optional[int] = None,
           pid_after: Optional[int] = None):
    """pre_log/runtime/operations_YYYYMMDD.jsonl chmod 600 per-day rotation."""
    try:
        _RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_RUNTIME_LOG_DIR), 0o700)
        except OSError:
            pass
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = _RUNTIME_LOG_DIR / f"operations_{date_str}.jsonl"
        new_file = not log_file.exists()
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "target_id": target_id,
            "action": action,
            "initiated_by": initiated_by,
            "result": result,
            "error": error[:200] if error else "",
            "pid_before": pid_before,
            "pid_after": pid_after,
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


def _write_crash_finding(target_id: str, info: dict) -> Optional[Path]:
    """crash → finding pre/findings/HIGH-runtime-crash-{target}-{ts}.md.
    findings 写到 pre 项目目录 (主 finding 处理器在 pre stop hook).
    """
    try:
        findings_dir = _FINDINGS_HOME / "pre" / "pre" / "findings"
        findings_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        fp = findings_dir / f"HIGH-runtime-crash-{target_id}-{ts}.md"
        with open(fp, "w", encoding="utf-8") as f:
            f.write(f"# HIGH: runtime crash — {target_id}\n\n")
            f.write(f"- ts: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"- target_id: {target_id}\n")
            f.write(f"- detected_via: {info.get('detected_via', 'unknown')}\n")
            f.write(f"- last_pid: {info.get('pid')}\n")
            f.write(f"- tmux_session: {info.get('tmux_session', '')}\n")
            f.write(f"- port: {info.get('port', '')}\n\n")
            f.write("## 检测路径\n\n")
            f.write(f"- tmux_session_alive: {info.get('tmux_session_alive')}\n")
            f.write(f"- pid_alive: {info.get('pid_alive')}\n")
            f.write(f"- port_listening: {info.get('port_listening')}\n\n")
            f.write("## 建议\n\n")
            f.write(f"- 若需 auto-restart, restart_policy=on_crash\n")
            f.write(f"- 手动: `curl -X POST .../api/v1/runtime/process/restart -d '{{\"target_id\":\"{target_id}\"}}'`\n")
        return fp
    except OSError:
        return None


def _alert_user_default(target_id: str, info: dict):
    """crash → user.default critical alert. 复用 notify_abstract HTTP 路径."""
    try:
        import urllib.request, urllib.error
        master_url = os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500").rstrip("/")
        token = _resolve_token("hook")
        body = {
            "kind": "alert",
            "from_agent": "local.cli-claude-code-local.pre",
            "from_role": "platform",
            "payload": {
                "text": (f"[runtime crash] {target_id}\n"
                         f"detected_via: {info.get('detected_via', 'unknown')}\n"
                         f"tmux_alive: {info.get('tmux_session_alive')} / "
                         f"pid_alive: {info.get('pid_alive')} / "
                         f"port: {info.get('port_listening')}"),
                "severity": "critical",
                "priority": "critical",
            },
        }
        # alert 不在 SEND_KIND_WHITELIST, 但 virtual agent 路径接受任意 kind. 用 chat 兜底.
        body["kind"] = "chat"
        req = urllib.request.Request(
            f"{master_url}/api/v1/agents/user.default/send",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        _NO_PROXY_OPENER.open(req, timeout=5).read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        pass  # fail-safe


def start(target_id: str, force: bool = False, initiated_by: str = "?") -> dict:
    """spawn target 进 tmux session (如有 tmux_session 字段) 或 subprocess detached.
    返 {ok, pid, action, error?}.
    """
    target = get_target(target_id)
    if not target:
        _audit("start", target_id, "missing_target", initiated_by=initiated_by)
        return {"ok": False, "error": "missing_target", "target_id": target_id}
    h = health(target_id)
    pid_before = h.get("pid")
    if h.get("alive") and not force:
        _audit("start", target_id, "already_alive", initiated_by=initiated_by,
               pid_before=pid_before, pid_after=pid_before)
        return {"ok": True, "action": "noop", "reason": "already_alive",
                "pid": pid_before, **h}
    cmd = target.get("start_command") or ""
    cwd = _expand_path(target.get("cwd") or os.path.expanduser("~"))
    tmux_session = target.get("tmux_session") or ""
    pid_file = target.get("pid_file") or ""
    # opt-in tmux startup rc (proxy + JP egress 验证)
    # target 加 "tmux_rc": true 才 wrap; 默认 false 兼容 daemon (master/node/cron)
    tmux_rc_enabled = bool(target.get("tmux_rc"))
    try:
        if tmux_session:
            tmux = find_tmux()
            # kill 老 session (可能是 dead session) 再起
            subprocess.run([tmux, "kill-session", "-t", tmux_session],
                           capture_output=True, timeout=3)
            launch_cmd = cmd
            if tmux_rc_enabled:
                rc_path = _resolve_tmux_rc()
                if rc_path:
                    launch_cmd = f'bash -ic "source {rc_path} && exec {cmd}"'
            r = subprocess.run(
                [tmux, "new-session", "-d", "-s", tmux_session, "-c", cwd, launch_cmd],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                err = r.stderr.strip() or "tmux new-session failed"
                _audit("start", target_id, "failed", initiated_by=initiated_by, error=err)
                return {"ok": False, "error": err, "target_id": target_id}
            # 拿 pid: tmux pane pid (subprocess 的 pid 不是 cmd 实际 pid)
            r2 = subprocess.run(
                [tmux, "list-panes", "-t", tmux_session, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=3,
            )
            pid = int(r2.stdout.strip()) if r2.returncode == 0 and r2.stdout.strip() else 0
        else:
            # 无 tmux: subprocess detached
            proc = subprocess.Popen(
                ["bash", "-lc", cmd], cwd=cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            pid = proc.pid
        if pid:
            _write_pid_file(pid_file, pid)
        _audit("start", target_id, "ok", initiated_by=initiated_by,
               pid_before=pid_before, pid_after=pid)
        return {"ok": True, "action": "started", "pid": pid,
                "tmux_session": tmux_session, "target_id": target_id}
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        _audit("start", target_id, "failed", initiated_by=initiated_by, error=err)
        return {"ok": False, "error": err, "target_id": target_id}


def stop(target_id: str, force: bool = False, initiated_by: str = "?") -> dict:
    """优雅 stop tmux session 或 SIGTERM pid. force=True 走 kill-session / SIGKILL."""
    target = get_target(target_id)
    if not target:
        _audit("stop", target_id, "missing_target", initiated_by=initiated_by)
        return {"ok": False, "error": "missing_target"}
    h = health(target_id)
    pid_before = h.get("pid")
    tmux_session = target.get("tmux_session") or ""
    pid_file = target.get("pid_file") or ""
    if not h.get("alive"):
        _audit("stop", target_id, "already_stopped", initiated_by=initiated_by,
               pid_before=pid_before)
        return {"ok": True, "action": "noop", "reason": "already_stopped"}
    try:
        if tmux_session and h.get("tmux_session_alive"):
            tmux = find_tmux()
            subprocess.run([tmux, "kill-session", "-t", tmux_session],
                           capture_output=True, timeout=3)
        elif pid_before:
            sig = 9 if force else 15  # SIGKILL or SIGTERM
            os.kill(pid_before, sig)
        # 清 pid file
        if pid_file:
            pf = _LOG_DIR / pid_file[len("pre_log/"):] if pid_file.startswith("pre_log/") else Path(_expand_path(pid_file))
            try:
                if pf.exists():
                    pf.unlink()
            except OSError:
                pass
        _audit("stop", target_id, "ok", initiated_by=initiated_by,
               pid_before=pid_before, pid_after=None)
        return {"ok": True, "action": "stopped", "target_id": target_id}
    except (OSError, subprocess.SubprocessError, ProcessLookupError) as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        _audit("stop", target_id, "failed", initiated_by=initiated_by, error=err)
        return {"ok": False, "error": err}


def restart(target_id: str, initiated_by: str = "?") -> dict:
    """stop + start. 失败 stop 不阻断 start (force start)."""
    s_result = stop(target_id, force=False, initiated_by=initiated_by)
    time.sleep(0.5)
    return start(target_id, force=True, initiated_by=initiated_by)


def scan_crashes(initiated_by: str = "watchdog_cron") -> list[dict]:
    """cron 兜底入口. 检查所有 enabled target 是否 crashed.
    crash detected → 写 finding + alert. 不主动 restart (除非 restart_policy=on_crash).
    返 list of crash info.
    """
    crashes = []
    cfg = load_config()
    for tid, t in (cfg.get("targets") or {}).items():
        if not t.get("enabled"):
            continue
        h = health(tid)
        if not h.get("alive"):
            info = {**h, "detected_via": "watchdog_cron",
                    "tmux_session": t.get("tmux_session"),
                    "port": (t.get("health_check") or {}).get("port")}
            crashes.append(info)
            _audit("crash_detected", tid, "detected", initiated_by=initiated_by)
            _write_crash_finding(tid, info)
            _alert_user_default(tid, info)
            # restart_policy=on_crash 时自动 restart
            if t.get("restart_policy") == "on_crash":
                r = restart(tid, initiated_by="watchdog_auto_restart")
                _audit("auto_restart", tid, "ok" if r.get("ok") else "failed",
                       initiated_by="watchdog_auto_restart",
                       error=r.get("error", ""))
    return crashes
