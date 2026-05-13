"""
cycle_alert — freerun cycle stop reverse notification.

freerun agent (Phase A: agent-research) cycle stop 时检测 result_type, 反向通知 advisory manager
(Phase A: fn_quant_strat) + alert user.default. 接收方处理 ack 异步 with timeout (HC-G11
vacuous truth 第四次法则正式升格核心): 30min 未 ack → finding HIGH.

[agent-research-only hack 自 待 ≥3 agent 升级通用路由表 — 见 
 agent-gov verdict G9 + 同模式]

API:
  load_cycle_routing() -> dict
  match_route(agent_id, result_type) -> dict | None
  detect_cycle_stop_result(transcript_path, agent_id) -> dict | None
  send_cycle_alert(agent_id, result_type, result_detail, context, transcript_path?) -> alert_id | None
  handle_ack(alert_id, decision, eta?) -> bool
  check_ack_timeouts() -> list[str] # alert_id list of timeouts

HC-PRE-1 stdlib only. fail-safe: 任何异常 silent skip.
"""
from __future__ import annotations
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from common.paths import PRE_ROOT, PRE_RULE_ROOT, PRE_LOG_ROOT
from typing import Optional

# Loopback master call: direct, bypass proxy env (Surge etc.)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------- 路径常量 ----------
_ROUTE_PATH = Path(os.environ.get(
    "PRE_CYCLE_ROUTING",
    str(Path(PRE_RULE_ROOT) / "cycle_routing.json"),
))
_LOG_DIR = Path(os.environ.get(
    "PRE_LOG_DIR",
    PRE_LOG_ROOT,
))
_STATE_DIR = _LOG_DIR / "cycle_alert"
_STATE_PATH = _STATE_DIR / "state.json"

# Phase B 灰度: NODE_URL 优先 (经 node loopback proxy 中转), fallback MASTER_URL.
# Phase D 后 MASTER_URL fallback 移除, agent 强制走 node.
_MASTER_URL = (
    os.environ.get("PRE_NODE_URL")
    or os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500")
)
# token: lazy resolve from ~/.pre/env via token_resolver (PR3).
# 不预存常量; PRE_HOOK_SECRET 未设时 import 不 fail, 仅请求时 raise + 上游 except 兜底.
try:
    from src.common.token_resolver import resolve as _resolve_token  # hook context
except ImportError:
    from common.token_resolver import resolve as _resolve_token  # master context

# mtime cache
_ROUTE_CACHE: dict = {"mtime": 0.0, "cfg": None}

# G3 默认 ack timeout 30min (路由表可覆盖)
_DEFAULT_ACK_TIMEOUT_SEC = 1800.0


def load_cycle_routing() -> dict:
    """G2 mtime hot reload, fail-safe empty on error."""
    try:
        if not _ROUTE_PATH.exists():
            return {"version": 1, "routes": []}
        mtime = _ROUTE_PATH.stat().st_mtime
        if _ROUTE_CACHE["cfg"] is not None and _ROUTE_CACHE["mtime"] == mtime:
            return _ROUTE_CACHE["cfg"]
        with open(_ROUTE_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        _ROUTE_CACHE["cfg"] = cfg
        _ROUTE_CACHE["mtime"] = mtime
        return cfg
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "routes": []}


def match_route(agent_id: str, result_type: str) -> Optional[dict]:
    """match agent_id against PCRE patterns. 返第一个命中 route or None.
    [agent-research-only hack 自 ]"""
    cfg = load_cycle_routing()
    for route in cfg.get("routes") or []:
        pat = route.get("agent_id_pattern", "")
        if not pat:
            continue
        try:
            if not re.match(pat, agent_id):
                continue
        except re.error:
            continue
        rt = route.get("result_type", "*")
        if rt != "*" and rt != result_type:
            continue
        return route
    return None


def detect_cycle_stop_result(transcript_path: str,
                              agent_id: str) -> Optional[dict]:
    """读 transcript 末几条 line 检 result_type.
    返 {result_type, result_detail, context} or None.
    fail-safe.

    result_type ∈ {no_task, complete, error, stuck}. 启发式简化:
      - "no task available" / "no_task_available" → no_task
      - "completed" / "task done" → complete
      - "error" / "failed" / "exception" → error
      - 其他 → None (不发 alert, 严禁 vacuous truth)
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    try:
        # 读末 50 行 (transcript 是 jsonl)
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()[-50:]
    except (OSError, ValueError):
        return None
    blob = "\n".join(lines).lower()
    result_type = None
    if "no_task_available" in blob or "no task available" in blob \
            or "没有任务" in blob or "no task" in blob[-2000:]:
        result_type = "no_task"
    elif "task done" in blob or "completed" in blob[-1500:] \
            or "task complete" in blob:
        result_type = "complete"
    elif "exception" in blob or "error:" in blob or "failed" in blob[-1500:]:
        result_type = "error"
    if result_type is None:
        return None
    # result_detail (≤500 chars 净化)
    detail_lines = lines[-5:]
    detail = "\n".join(detail_lines)[-500:]
    # 净化控制字符 (复用 思路, 简化)
    detail = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", detail)
    return {
        "result_type": result_type,
        "result_detail": detail,
        "context": {
            "transcript_lines_scanned": len(lines),
            "agent_id": agent_id,
        },
    }


def _read_state() -> dict:
    try:
        if not _STATE_PATH.exists():
            return {"alerts": {}}
        with open(_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"alerts": {}}


def _write_state(state: dict):
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_STATE_DIR), 0o700)
        except OSError:
            pass
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(str(_STATE_PATH), 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _post_master(path: str, body: dict, timeout: float = 8.0) -> tuple[bool, dict]:
    try:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            _MASTER_URL.rstrip("/") + path,
            data=data,
            headers={"Authorization": f"Bearer {_resolve_token('hook')}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            OSError, ValueError, RuntimeError) as e:
        return False, {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def send_cycle_alert(agent_id: str, result_type: str,
                      result_detail: str, context: Optional[dict] = None) -> Optional[str]:
    """发 cycle_alert 给 target_manager + alert user.default (G1 + G3).
    返 alert_id or None (no route / send fail).
    [agent-research-only hack 自 ]"""
    route = match_route(agent_id, result_type)
    if route is None:
        return None  # 无匹配路由, 严禁 silent merge — 但缺 route 是 expected (非 agent-research agent)
    alert_id = uuid.uuid4().hex
    sent_ts = time.time()
    target_manager = route.get("target_manager", "")
    if not target_manager:
        _audit_finding(alert_id, agent_id, "HIGH-cycle-alert-route-failed",
                       "target_manager_empty", route)
        return None
    # node_id 推断
    node_id = agent_id.split(".")[0] if "." in agent_id else "local"
    # G1 cycle_alert payload v1
    payload = {
        "alert_id": alert_id,
        "agent_id": agent_id,
        "node_id": node_id,
        "cycle_stop_ts": sent_ts,
        "result_type": result_type,
        "result_detail": (result_detail or "")[:500],
        "context": context or {},
    }
    text = (f"[cycle_alert] agent={agent_id} result={result_type} "
            f"alert_id={alert_id}\n\nresult_detail (≤500c):\n{result_detail}\n\n"
            f"context: {json.dumps(context or {}, ensure_ascii=False)}\n\n"
            f"ack required: kind=cycle_alert_ack payload "
            f'{{alert_id: "{alert_id}", decision: PASS|FAIL|PENDING, eta?, '
            f'spec_paths?, evaluator_ts}} ≤30min '
            f"否则 finding HIGH-cycle-alert-no-ack-{target_manager} (HC-G11 vacuous truth).")
    # 1. 发 cycle_alert kind=cycle_alert
    body_alert = {
        "kind": "cycle_alert",
        "priority": 0,
        "payload": {"text": text, **payload},
    }
    ok_a, info_a = _post_master(f"/api/v1/agents/{target_manager}/send", body_alert)
    if not ok_a:
        _audit_finding(alert_id, agent_id,
                       "HIGH-cycle-alert-deliver-failed",
                       f"send_to_target_failed: {info_a.get('error', '')}",
                       {"target_manager": target_manager})
    # 2. alert user.default (复用 , kind=chat priority=high)
    if route.get("alert_user_default"):
        body_user = {
            "kind": "chat",
            "priority": 0,  # priority text in payload
            "payload": {
                "text": (f"[cycle_alert] agent={agent_id} "
                         f"result_type={result_type} → routed {target_manager} "
                         f"alert_id={alert_id} (ack ≤30min)"),
                "priority_label": "high",
            },
        }
        _post_master("/api/v1/agents/user.default/send", body_user, timeout=5.0)
    # 3. 写 state.json
    state = _read_state()
    alerts = state.get("alerts") or {}
    alerts[alert_id] = {
        "agent_id": agent_id,
        "target_manager": target_manager,
        "result_type": result_type,
        "sent_ts": sent_ts,
        "ack_timeout_sec": float(route.get("ack_timeout_seconds",
                                              _DEFAULT_ACK_TIMEOUT_SEC)),
        "status": "pending",
        "ack_required": bool(route.get("ack_required", True)),
    }
    state["alerts"] = alerts
    _write_state(state)
    return alert_id


def handle_ack(alert_id: str, decision: str = "PASS",
               eta: Optional[str] = None,
               spec_paths: Optional[list] = None) -> bool:
    """更新 state.json acked. fail-safe."""
    if decision not in ("PASS", "FAIL", "PENDING"):
        return False  # G3 e: 必含 PASS/FAIL/PENDING
    if decision == "PENDING" and not eta:
        return False  # G3 e: PENDING 必含 ETA
    state = _read_state()
    alerts = state.get("alerts") or {}
    if alert_id not in alerts:
        return False
    alerts[alert_id]["ack_ts"] = time.time()
    alerts[alert_id]["decision"] = decision
    if eta:
        alerts[alert_id]["eta"] = eta
    if spec_paths:
        alerts[alert_id]["spec_paths"] = spec_paths
    alerts[alert_id]["status"] = "acked"
    state["alerts"] = alerts
    _write_state(state)
    return True


def check_ack_timeouts() -> list[str]:
    """扫 state.json pending 超 ack_timeout_sec → status=timeout +
    finding HIGH-cycle-alert-no-ack-{target} + alert user.default critical.
    返 newly-timed-out alert_id list. fail-safe.
    """
    state = _read_state()
    alerts = state.get("alerts") or {}
    now = time.time()
    new_timeouts = []
    for aid, info in alerts.items():
        if info.get("status") != "pending":
            continue
        sent_ts = info.get("sent_ts", 0.0)
        timeout_sec = info.get("ack_timeout_sec", _DEFAULT_ACK_TIMEOUT_SEC)
        if (now - sent_ts) <= timeout_sec:
            continue
        # timeout
        info["status"] = "timeout"
        info["timeout_ts"] = now
        new_timeouts.append(aid)
        target = info.get("target_manager", "?")
        _audit_finding(aid, info.get("agent_id", "?"),
                       f"HIGH-cycle-alert-no-ack-{target}",
                       f"ack_timeout sent_ts={sent_ts:.0f} elapsed={now-sent_ts:.0f}s "
                       f"timeout_threshold={timeout_sec:.0f}s",
                       info)
        # 升级 alert user.default critical
        body_user = {
            "kind": "chat",
            "priority": 0,
            "payload": {
                "text": (f"[cycle_alert TIMEOUT] alert_id={aid} target={target} "
                         f"未在 {timeout_sec:.0f}s 内 ack — HC-G11 vacuous truth 反驳: "
                         f"agent={info.get('agent_id', '?')} result_type={info.get('result_type', '?')} "
                         "请人工介入"),
                "priority_label": "critical",
            },
        }
        _post_master("/api/v1/agents/user.default/send", body_user, timeout=5.0)
    if new_timeouts:
        state["alerts"] = alerts
        _write_state(state)
    return new_timeouts


def _audit_finding(alert_id: str, agent_id: str, finding_slug: str,
                    reason: str, ctx: dict):
    """G6 audit: 走 messages 表已经在 send 路径自动落. 这里再写 finding 文件."""
    try:
        # finding 写到 pre/pre/findings/ (本项目 finding 目录)
        # Phase A agent-research-only, 写 pre 的 findings (而非 agent-research 项目)
        findings_dir = Path(PRE_ROOT) / "pre" / "findings"
        findings_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        slug = re.sub(r"[^a-zA-Z0-9_\-]", "-", finding_slug)[:80]
        fpath = findings_dir / f"{slug}-{ts}-{alert_id[:8]}.md"
        body = f"""# {finding_slug}

ts: {datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
alert_id: {alert_id}
agent_id: {agent_id}

reason: {reason}

context:
```json
{json.dumps(ctx, ensure_ascii=False, indent=2)}
```

---
来源: cycle_alert.py ( Phase A)
"""
        fpath.write_text(body, encoding="utf-8")
    except OSError:
        pass


# ---------- 入口 hook (stop_analyzer 调) ----------

def cycle_alert_hook(agent_id: str, transcript_path: str) -> Optional[str]:
    """stop_analyzer 末尾调. 检 transcript → 匹配路由 → send.
    fail-safe: 任何异常 silent skip. 返 alert_id or None.
    [agent-research-only hack 自 ]"""
    # lazy 检 ack timeouts (顺手, 不开新 cron)
    try:
        check_ack_timeouts()
    except Exception:  # noqa: BLE001
        pass
    try:
        result = detect_cycle_stop_result(transcript_path, agent_id)
        if result is None:
            return None
        # 仅匹配路由的 agent 才发
        route = match_route(agent_id, result["result_type"])
        if route is None:
            return None
        return send_cycle_alert(agent_id, result["result_type"],
                                  result.get("result_detail", ""),
                                  result.get("context", {}))
    except Exception:  # noqa: BLE001 — fail-safe
        return None
