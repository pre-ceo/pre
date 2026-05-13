#!/usr/bin/env python3
"""
pre/scripts/usage_probe_once.py — 单次 usage 抓取脚本 (cron 调).

事件触发模型: cron daemon 按 schedules.json 的 pre_usage_probe entry 跑此脚本,
不在 master event loop 内 polling.

流程:
1. 调 _probe_all_async() 抓 3 家 cli (0 LLM cost)
2. POST /api/v1/usage/snapshot 给 master 更新 registry.usage (sticky 保护已在 master 端)
3. 计算 severity 变化 (prev vs cur), 跨过阈值 → POST /api/v1/usage/event push 事件
4. 健康检查: 连续 N 次失败的 sys_* session → kill + respawn (cool-down 5min)

usage:
  uv run python scripts/usage_probe_once.py
  uv run python scripts/usage_probe_once.py --providers claude,gemini
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from master.usage_prober import _PROBE_SPECS, _probe_all_async  # noqa: E402
from common.token_resolver import resolve as _resolve_token  # noqa: E402
from common.paths import PRE_RULE_ROOT, PRE_LOG_ROOT  # noqa: E402

MASTER_URL = os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500")
MASTER_TOKEN = _resolve_token("hook")  # ~/.pre/env::PRE_HOOK_SECRET

# Loopback master call: bypass proxy env (Surge etc.)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_post(url: str, body: dict, timeout: float = 10.0) -> dict | None:
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Bearer {MASTER_TOKEN}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as e:
        print(f"[usage-probe] http_post {url} failed: {e}", flush=True)
        return None


def _http_get(url: str, timeout: float = 5.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {MASTER_TOKEN}"})
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None


def _compute_severity(provider_data: dict) -> str:
    """根据 %used 推 severity. 跨家通用:
    - critical: 任一字段 >= 95
    - warning: 任一字段 >= 80
    - ok: 否则
    """
    if not isinstance(provider_data, dict):
        return "unknown"
    pcts = []
    if "session_percent_used" in provider_data:
        pcts.append(provider_data["session_percent_used"])
    if "week_percent_used" in provider_data:
        pcts.append(provider_data["week_percent_used"])
    if "models" in provider_data and isinstance(provider_data["models"], dict):
        for m in provider_data["models"].values():
            if isinstance(m, dict) and "percent_used" in m:
                pcts.append(m["percent_used"])
    # codex left -> used = 100 - left
    if "percent_left_5h" in provider_data:
        pcts.append(100 - provider_data["percent_left_5h"])
    if "percent_left_week" in provider_data:
        pcts.append(100 - provider_data["percent_left_week"])
    if not pcts:
        return "unknown"
    max_pct = max(pcts)
    if max_pct >= 95:
        return "critical"
    if max_pct >= 80:
        return "warning"
    return "ok"


_ALL_PROVIDERS = ("claude", "claude_foxbn", "gemini", "codex")


def _post_snapshot(data: dict, node_id: str = "local") -> dict | None:
    """POST 快照 → master usage_snapshot endpoint.
    - skipped/probe_inconclusive 等不入 body 让 master sticky 保留旧值
    - status='ok' 但缺 cur/week/reset 任一 = 半成品快照, quarantine 不发, master sticky 保留旧好数据
    - respawn 决策仍由 _do_respawn_pass 独立处理 (走 streak 路径)
    """
    body: dict = {"ts": time.time(), "node_id": node_id}
    severity: dict = {}
    for p in _ALL_PROVIDERS:
        pd = data.get(p)
        if pd is None:
            continue  # 此 provider 本次没 probe
        if isinstance(pd, dict) and pd.get("status") == "skipped":
            continue  # tmux session 不存在, 不要污染 master 旧值
        if isinstance(pd, dict) and pd.get("status") in _HEALTHY_STATUSES:
            missing = _missing_required_fields(p, pd)
            if missing:
                print(f"[snapshot-quarantine] {p} status={pd.get('status')} but missing "
                      f"{missing} → skip upsert (master sticky 保留旧值)", flush=True)
                continue
        body[p] = pd
        severity[p] = _compute_severity(pd)
    if severity:
        body["severity"] = severity
    return _http_post(MASTER_URL.rstrip("/") + "/api/v1/usage/snapshot", body)


def _post_event(provider: str, severity: str, prev_severity: str, provider_data: dict):
    """severity 变化时 push event (event-driven). audit + 后续推 fn_ops_account."""
    body = {
        "ts": time.time(),
        "provider": provider,
        "severity": severity,
        "prev_severity": prev_severity,
        "used_summary": _compute_severity_summary(provider_data),
    }
    return _http_post(MASTER_URL.rstrip("/") + "/api/v1/usage/event", body)


def _compute_severity_summary(provider_data: dict) -> dict:
    """脱敏摘要: 仅 used % + window 元数据, 不带 raw token."""
    out = {"status": provider_data.get("status")}
    for k in ("session_percent_used", "week_percent_used", "session_reset", "week_reset",
              "active_model", "active_model_percent_used", "models", "models_limited",
              "percent_left_5h", "reset_5h", "percent_left_week", "reset_week",
              "model", "plan"):
        if k in provider_data:
            out[k] = provider_data[k]
    return out


# ============================================================
# sys_* probe 健康检查 + 自动 respawn
# 复用 probe 失败信号做健康检查, 不另起 watchdog.
# streak counter 持久化 ~/.pre/data/probe_health/<provider>.json
# ============================================================

_HEALTH_DIR = Path(os.environ.get(
    "PRE_PROBE_HEALTH_DIR",
    str(Path.home() / ".pre" / "data" / "probe_health"),
))
_RESPAWN_AUDIT_LOG = Path(os.environ.get(
    "PRE_PROBE_HEALTH_LOG",
    str(Path(PRE_LOG_ROOT) / "probe_health_respawn.log"),
))
_FAIL_STATUSES = {"error", "probe_inconclusive", "unknown", "status_bar_only"}
_HEALTHY_STATUSES = {"ok", "limit_reached", "near_limit"}
_FAIL_STREAK_THRESHOLD = 3       # 连续失败 N 次触发 respawn (cron 900s × 3 = 45min 容忍)
_RESPAWN_COOLDOWN_S = 300        # respawn 后至少 5min 才能再次 respawn

# 即使 status='ok', 缺 cur/week/reset 任一 = 格式没出来, 视为失败.
# per-provider 必须字段 (任一缺即降级为 probe_inconclusive).
_REQUIRED_FIELDS = {
    # claude /usage: cur=session_percent_used, week=week_percent_used, reset 各对应
    "claude": ("session_percent_used", "week_percent_used",
               "session_reset", "week_reset"),
    # codex /status: cur=percent_left_5h, week=percent_left_week, reset 各对应
    "codex": ("percent_left_5h", "percent_left_week",
              "reset_5h", "reset_week"),
    # gemini /model 没 weekly 概念, dialog 数据 (models dict + reset_at) 是核心要求.
    # dialog 打开时会盖住底部状态栏, 所以 active_model_percent_used 可能抓不到 — 不强求.
    # 只要 models 字段在就是完整数据. 状态栏 only (无 models) 视为 cli 异常.
    "gemini": ("models",),
}


def _missing_required_fields(provider: str, pd: dict) -> list[str]:
    """返 provider 必须字段中 pd 缺/None/空的列表. 空 = healthy."""
    req = _REQUIRED_FIELDS.get(provider, ())
    miss = []
    for f in req:
        v = pd.get(f)
        if v is None or v == "" or (isinstance(v, (list, dict)) and not v):
            miss.append(f)
    # gemini 额外: 必须 models dict 任一带 reset_at (dialog 数据是核心要求)
    if provider == "gemini" and not miss:
        models = pd.get("models") or {}
        any_reset = any(m.get("reset_at") for m in models.values()
                        if isinstance(m, dict))
        if not any_reset:
            miss.append("reset_at")
    return miss


def _streak_path(provider: str, node_id: str) -> Path:
    return _HEALTH_DIR / f"{node_id}_{provider}.json"


def _load_streak(provider: str, node_id: str) -> dict:
    p = _streak_path(provider, node_id)
    if not p.is_file():
        return {"fail_streak": 0, "last_status": None, "last_ts": 0.0,
                "last_respawn_ts": 0.0}
    try:
        with p.open("r", encoding="utf-8") as f:
            d = json.load(f)
        return {
            "fail_streak": int(d.get("fail_streak", 0)),
            "last_status": d.get("last_status"),
            "last_ts": float(d.get("last_ts") or 0.0),
            "last_respawn_ts": float(d.get("last_respawn_ts") or 0.0),
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {"fail_streak": 0, "last_status": None, "last_ts": 0.0,
                "last_respawn_ts": 0.0}


def _save_streak(provider: str, node_id: str, state: dict):
    try:
        _HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(_HEALTH_DIR, 0o700)
        except OSError:
            pass
        p = _streak_path(provider, node_id)
        with p.open("w", encoding="utf-8") as f:
            json.dump(state, f)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError as e:
        print(f"[probe-health] save_streak {provider} failed: {e}", flush=True)


def _audit_respawn(node_id: str, provider: str, action: str, reason: str,
                    streak: int, result: str):
    """append-only audit log."""
    try:
        _RESPAWN_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (f"{ts} node={node_id} provider={provider} action={action} "
                f"reason={reason} streak={streak} result={result}\n")
        with _RESPAWN_AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
        try:
            os.chmod(_RESPAWN_AUDIT_LOG, 0o600)
        except OSError:
            pass
    except OSError as e:
        print(f"[probe-health] audit append failed: {e}", flush=True)


def _resolve_spawn_rc() -> str | None:
    """spawn rc 解析: $PRE_SPAWN_RC > pre_rule/spawn.rc > pre/scripts/spawn.rc.
    跨 node fallback 含 /root/workspace/.
    pre_rule/spawn.rc 是 user 配置层; pre/scripts/spawn.rc 是 pre 内置 minimal fallback."""
    env = os.environ.get("PRE_SPAWN_RC")
    candidates = []
    if env:
        candidates.append(env)
    candidates += [
        os.path.join(PRE_RULE_ROOT, "spawn.rc"),
        "/root/workspace/pre_rule/spawn.rc",
        str(PROJECT_ROOT / "scripts" / "spawn.rc"),
        "/root/workspace/pre/scripts/spawn.rc",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _respawn(provider: str, force_kill: bool = False) -> tuple[bool, str]:
    """spawn 一个 sys_* tmux session, wrap spawn.rc.
    零配置: cli 命令直接取 _PROBE_SPECS[provider]['spawn_cli'], cwd 用 $HOME.
    sys_* 是平台 monitor session, 不需要 user 建 $PRE_AGENT_HOME/sys_*/ 项目目录.
    force_kill=True: session 已存在也先 kill 再重起 (streak_threshold 路径,
    cli 异常时必须重起)."""
    spec = _PROBE_SPECS.get(provider) or {}
    session = spec.get("session")
    spawn_cli = spec.get("spawn_cli")
    if not session or not spawn_cli:
        return False, f"unknown provider {provider} or missing spawn_cli"
    rc = _resolve_spawn_rc()
    if not rc:
        return False, "spawn.rc not found"
    # sys_* 用 pre 自己的 workdir, 不污染 user 仓库; chmod 700 防其他进程窥探.
    cwd = os.path.join(os.path.expanduser("~"), ".pre", "sys_workdir")
    try:
        os.makedirs(cwd, mode=0o700, exist_ok=True)
    except OSError:
        cwd = os.path.expanduser("~")  # fallback: home dir
    wrapped = f'bash -ic "source {rc} && exec {spawn_cli}"'
    try:
        check = subprocess.run(["tmux", "has-session", "-t", f"={session}"],
                                capture_output=True, timeout=5)
        if check.returncode == 0:
            if force_kill:
                subprocess.run(["tmux", "kill-session", "-t", f"={session}"],
                               capture_output=True, timeout=5)
                time.sleep(1)  # 让 tmux 释放 session 名
            else:
                # session_not_found 路径下意外发现已存在 (concurrent cron) → 视为 ok
                return True, "session already exists"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-c", cwd, wrapped],
            capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "tmux new-session timeout"
    except FileNotFoundError:
        return False, "tmux not found in PATH"
    if result.returncode != 0:
        return False, (f"tmux exit {result.returncode}: "
                       f"{result.stderr.decode('utf-8', errors='replace')[:200]}")
    # rc 可能含 egress 校验, 失败时 sleep + exit 1, 进程 5s 内会消失. 等 7s 验.
    time.sleep(7)
    try:
        check = subprocess.run(["tmux", "has-session", "-t", f"={session}"],
                                capture_output=True, timeout=5)
        if check.returncode != 0:
            return False, "session died after spawn (rc check failed?)"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, "post-spawn check failed"

    # claude code v2 第一次进入新 cwd 会弹 "Quick safety check / Yes, I trust this folder",
    # 默认指针在 1. Yes, 直接 Enter 即可 confirm. 必须 confirm 否则 /usage 抓不到.
    # gemini / codex 没这个弹窗, 此 capture 命中也无害.
    try:
        cap = subprocess.run(["tmux", "capture-pane", "-t", session, "-p"],
                             capture_output=True, text=True, timeout=3)
        pane_text = cap.stdout or ""
        if "trust this folder" in pane_text or "Quick safety check" in pane_text:
            subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                           capture_output=True, timeout=3)
            time.sleep(2)
            print(f"[probe-spawn] {provider}: auto-confirmed trust dialog", flush=True)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return True, "ok"


def _process_health(provider: str, node_id: str, status: str | None,
                    skipped_reason: str | None) -> tuple[bool, str, int]:
    """决策是否 respawn. 返 (do_respawn, reason, streak_after)."""
    state = _load_streak(provider, node_id)
    now = time.time()

    # 健康 → 清 streak
    if status in _HEALTHY_STATUSES:
        if state["fail_streak"] != 0:
            print(f"[probe-health] {provider} healthy ({status}), clear streak "
                  f"(was {state['fail_streak']})", flush=True)
        state.update({"fail_streak": 0, "last_status": status, "last_ts": now})
        _save_streak(provider, node_id, state)
        return False, "healthy", 0

    # session not found → 立即 respawn (强信号)
    if status == "skipped" and skipped_reason and "not found" in skipped_reason:
        # cool-down: 防 spawn fail 时风暴 (e.g. rc 校验持续失败)
        if now - state["last_respawn_ts"] < _RESPAWN_COOLDOWN_S:
            remain = int(_RESPAWN_COOLDOWN_S - (now - state["last_respawn_ts"]))
            print(f"[probe-health] {provider} session-missing in cool-down "
                  f"({remain}s remain), skip", flush=True)
            return False, "cooldown", state["fail_streak"]
        return True, "session_not_found", state["fail_streak"]

    # config disabled / 其他 skipped → 不动 (业务关闭, 不重启)
    if status == "skipped":
        return False, "config_skipped", state["fail_streak"]

    # 失败状态 → streak += 1, 阈值触发
    if status in _FAIL_STATUSES:
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        state["last_status"] = status
        state["last_ts"] = now
        _save_streak(provider, node_id, state)
        if state["fail_streak"] >= _FAIL_STREAK_THRESHOLD:
            if now - state["last_respawn_ts"] < _RESPAWN_COOLDOWN_S:
                remain = int(_RESPAWN_COOLDOWN_S - (now - state["last_respawn_ts"]))
                print(f"[probe-health] {provider} streak={state['fail_streak']} "
                      f"≥{_FAIL_STREAK_THRESHOLD} but cool-down ({remain}s remain), skip",
                      flush=True)
                return False, "cooldown", state["fail_streak"]
            return True, "streak_threshold", state["fail_streak"]
        return False, f"streak_{state['fail_streak']}", state["fail_streak"]

    # 未知状态 (e.g. None) → 不动, 防误判
    return False, f"unknown_status_{status}", state["fail_streak"]


def _do_respawn_pass(node_id: str, cur_data: dict, providers: list[str]):
    """main 末尾跑健康检查 + respawn."""
    for p in providers:
        pd = cur_data.get(p) or {}
        status = pd.get("status") if isinstance(pd, dict) else None
        skipped_reason = pd.get("skipped_reason") if isinstance(pd, dict) else None
        # status='ok' 但缺 cur/week/reset 任一 → 降级 probe_inconclusive,
        # 防"启动了 cli 但格式没出来"被误判健康.
        if status in _HEALTHY_STATUSES and isinstance(pd, dict):
            missing = _missing_required_fields(p, pd)
            if missing:
                print(f"[probe-health] {p} status={status} but missing fields "
                      f"{missing} → demote to probe_inconclusive", flush=True)
                status = "probe_inconclusive"
        do_respawn, reason, streak = _process_health(p, node_id, status,
                                                       skipped_reason)
        if not do_respawn:
            continue
        print(f"[probe-health] {p} → respawn (reason={reason} streak={streak})",
              flush=True)
        # streak_threshold = session 在但 cli 异常 → 必须 kill 旧 session 再起;
        # session_not_found = session 不在 → 直接起新的.
        force_kill = (reason == "streak_threshold")
        ok, msg = _respawn(p, force_kill=force_kill)
        result = "ok" if ok else f"fail({msg})"
        print(f"[probe-health] {p} respawn result: {result}", flush=True)
        _audit_respawn(node_id, p, "respawn", reason, streak, result)
        # 更新 last_respawn_ts (无论成功失败, cool-down 都生效防风暴)
        state = _load_streak(p, node_id)
        state["last_respawn_ts"] = time.time()
        if ok:
            # respawn 成功清 streak; 失败保留 streak (下轮再判)
            state["fail_streak"] = 0
            state["last_status"] = "respawned"
        _save_streak(p, node_id, state)


async def main(node_id: str = "local", providers: list[str] | None = None):
    started = time.time()
    plist_label = ",".join(providers) if providers else "all"
    print(f"[usage-probe] start ts={int(started)} node={node_id} providers={plist_label}",
          flush=True)

    # 拉前一次 severity 用于 event diff (从 master 拉)
    prev_data = _http_get(MASTER_URL.rstrip("/") + "/api/v1/usage") or {}
    track_providers = providers or list(_ALL_PROVIDERS)
    prev_severity = {
        p: _compute_severity(prev_data.get(p) or {})
        for p in track_providers
    }

    # 抓本次
    try:
        cur_data = await _probe_all_async(providers=providers)
    except Exception as e:
        print(f"[usage-probe] _probe_all_async failed: {e}", flush=True)
        return 1

    # log 跳过的 provider (远端 sys_gemini/sys_codex 可能不存在)
    for p in track_providers:
        pd = cur_data.get(p) or {}
        if pd.get("status") == "skipped":
            print(f"[usage-probe] {p}: skipped ({pd.get('skipped_reason')})", flush=True)

    cur_severity = {
        p: _compute_severity(cur_data.get(p) or {})
        for p in track_providers
    }

    # POST snapshot (always)
    snap_resp = _post_snapshot(cur_data, node_id=node_id)
    print(f"[usage-probe] snapshot resp: {snap_resp}", flush=True)

    # POST events (severity changed)
    events_posted = 0
    for p in track_providers:
        if cur_severity[p] != prev_severity[p]:
            print(f"[usage-probe] {p}: severity {prev_severity[p]} -> {cur_severity[p]} → post event",
                  flush=True)
            _post_event(p, cur_severity[p], prev_severity[p], cur_data.get(p) or {})
            events_posted += 1

    # 健康检查 + 自动 respawn (复用 probe 信号, 不另起 watchdog)
    try:
        _do_respawn_pass(node_id, cur_data, track_providers)
    except Exception as e:  # noqa: BLE001 — fail-safe, 不挡 cron 完成
        print(f"[probe-health] respawn pass failed: {e}", flush=True)

    elapsed = time.time() - started
    print(f"[usage-probe] done ({elapsed:.1f}s, {events_posted} events posted)", flush=True)
    return 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default=os.environ.get("PRE_NODE_ID", "local"),
                   help="node 标识 (local 默认). 远端 cron 跑时传 node 名.")
    p.add_argument("--providers", default=os.environ.get("PRE_PROBE_PROVIDERS", ""),
                   help="逗号分隔限定要抓的 provider (claude / gemini / codex). "
                        "默认空 = 全 3 家. 远端只装了 claude 时设 'claude'.")
    args = p.parse_args()
    providers = [x.strip() for x in args.providers.split(",") if x.strip()] or None
    sys.exit(asyncio.run(main(node_id=args.node_id, providers=providers)))
