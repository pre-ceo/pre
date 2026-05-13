"""
pre Master Server — asyncio TCP 多路复用 HTTP REST + WebSocket

单端口 (默认 19500):
  GET /api/v1/... → HTTP REST handler
  Upgrade: websocket + /node → Node 长连接 (JSON-RPC 2.0)
  Upgrade: websocket + /api/v1/stream → GUI push (本 phase 不实现)
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time
from typing import Optional

# 确保 src/ 在 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ws_lib import (
    parse_http_request, build_handshake_response,
    encode_frame, read_frame, send_text, send_to_writer, send_close,
    OPCODE_TEXT, OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG,
)
from message import Message
from master.persistence import MasterDB
from master.registry import Registry, NodeInfo, AgentInfo
# 加载 ~/.pre/env (single source by scripts/install.sh) — eager via token_resolver import.
from common import token_resolver  # noqa: F401 — side effect: load env

# ---------- pre 路径常量 (module-level single source, 给整个 file 用) ----------
# pre 仓库根 (脚本自定位, 允许的 __file__ 用法)
_PRE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# pre_rule / pre_log: 优先 env (install.sh 写入), fallback sibling 推算 (_PRE_ROOT/../).
# 不假设任何特定父目录, 完全靠 __file__ 自定位 + 可选 env override.
_PRE_RULE_ROOT = os.environ.get(
    "PRE_RULE_ROOT",
    os.path.normpath(os.path.join(_PRE_ROOT, "..", "pre_rule")),
)
_PRE_LOG_ROOT = os.environ.get(
    "PRE_LOG_DIR",
    os.path.normpath(os.path.join(_PRE_ROOT, "..", "pre_log")),
)


# ---------- 配置 ----------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19500
# : master.db relocation to ~/.pre/data/
# pre_log 纯临时日志, master.db 必搬走. 跟 ~/.pre/secrets/ 同 SoC.
# env override PRE_MASTER_DB 保留 (test 隔离 + 移植性).
DEFAULT_DB = os.path.join(
    os.path.expanduser("~"), ".pre", "data", "master.db",
)
DEFAULT_DB = os.path.normpath(DEFAULT_DB)

NODE_HEARTBEAT_TIMEOUT = 90.0   # 秒, 超时标 offline
HEARTBEAT_CHECK_INTERVAL = 15.0  # 秒, 心跳检查频率

# ---------- hardening ----------

# Origin 白名单 (POST + WS Upgrade); 没 Origin 视为 CLI 调用, 允许
# : 加 8090 (agent-fe feserver 默认端口) + 5174 (pre_ui 临时本地 server)
ORIGIN_WHITELIST = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:8090",
    "http://localhost:8090",
    "http://127.0.0.1:5174",
    "http://localhost:5174",
}

# send 接口允许的 kind
SEND_KIND_WHITELIST = {
    "command", "chat", "task_request", "evaluate_request",
    "verdict_reply", "task_verdict", "report",
    "user_direct",  # stop hook 留档 user 直发 prompt
    "proposal_chosen",  # 用户选 proposal 后注入 agent 的 audit 留档
    "cron_trigger",  # cron daemon 触发 audit (走 /api/v1/cron/trigger 端点, 不经 forward)
    "dispatch_brief",  # dispatcher 派 task_request 时调一次 LLM 生成的概览, audit_only
    "usage_event",  # usage_probe_once severity change push, audit_only forward 给 fn_ops_account
    "usage_snapshot",  # cron 驱动的 usage 快照 audit, 仅 db 留档
    # Phase A — freerun cycle stop reverse notification
    # [agent-research-only hack 自 待 ≥3 agent 升级通用路由表]
    "cycle_alert",       # G1 freerun agent → manager 反向通知
    "cycle_alert_ack",   # G1 manager → agent 接收方真处理 ack (HC-G11 vacuous truth)
    # step 3 dispatch pre mcp server 化
    # D5 严白 schema, audit_only 不 forward 给 node (mcp_server 子进程已是终点)
    "mcp_tool_call",     # mcp_server → master audit (kind whitelist 严白 + 限频 60/min/agent)
    "mcp_tool_response", # 反向 audit (Phase 2 后续, 现 step 3 可不必发)
}

# decide 接口允许的 key
DECIDE_KEY_WHITELIST = {"1", "2", "3", "Escape", "Enter", "Up", "Down"}

# virtual agents (user.default 等), 源码层 const, 严禁动态注册 (M4 + )
VIRTUAL_AGENTS = {"user.default"}

# chat priority 严格白名单 (M2 + )
PRIORITY_WHITELIST = {"critical", "high", "normal"}

# 限频 sliding window 三档 (M3 + )
# 本机使用 — 阈值上调至 1_000_000, 实际不会触发 429
# (保留 sliding window 逻辑供 audit/stats 复用, 不破坏代码结构)
RATE_LIMITS = {
    "critical": {"per_agent_per_min": 1_000_000, "global_per_min": 1_000_000},
    "high":     {"per_agent_per_min": 1_000_000, "global_per_min": 1_000_000},
    "normal":   {"per_agent_per_min": 1_000_000, "global_per_min": 1_000_000},
}
# in-memory: {(sender, priority): [ts, ...]} + ("__global__", priority): [ts, ...]
# sliding window 60s, 每次 send 进来 prune + count + decide
_RATE_WINDOWS: dict[tuple, list[float]] = {}

# pending decide retry, 按 pane_fp 字节指纹判定 (不靠 state/不靠 ws 回执).
# 流程: forward_decide 登记 entry (pane_fp=None) → 第一次心跳 cur_fp 当 baseline →
# 后续心跳 cur_fp != baseline 视为 ask 已消化弹出, == baseline 且 state==blocked_user 才重发.
# 触发器复用 report_activity 10s 心跳 (HC-PRE 不轮询).
_PENDING_DECIDES: dict[str, dict] = {}
_PENDING_DECIDE_MAX_TRIES = 3       # baseline 后最多重发 3 次 (≈30s)
_PENDING_DECIDE_MAX_AGE = 60.0      # 寿命 60s 强制弹出


def _rate_check(sender: str, priority: str) -> tuple[bool, str]:
    """sliding window 限频. 返 (allowed, reason)."""
    if priority not in RATE_LIMITS:
        return True, ""
    limits = RATE_LIMITS[priority]
    now = time.time()
    window_start = now - 60.0
    # per-agent
    key_a = (sender, priority)
    arr_a = _RATE_WINDOWS.setdefault(key_a, [])
    arr_a[:] = [t for t in arr_a if t > window_start]
    if len(arr_a) >= limits["per_agent_per_min"]:
        return False, f"rate_limited_per_agent ({len(arr_a)}/min >= {limits['per_agent_per_min']})"
    # global
    key_g = ("__global__", priority)
    arr_g = _RATE_WINDOWS.setdefault(key_g, [])
    arr_g[:] = [t for t in arr_g if t > window_start]
    if len(arr_g) >= limits["global_per_min"]:
        return False, f"rate_limited_global ({len(arr_g)}/min >= {limits['global_per_min']})"
    # accept, push ts
    arr_a.append(now)
    arr_g.append(now)
    return True, ""

# payload.text 拒绝出现的控制字符 (除 \n \t)
# 包括: ESC \x1b (色码 + tmux 控制), 其他 0x00-0x1f / \x7f
_FORBIDDEN_CTRL = set(chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0a))
_FORBIDDEN_CTRL.add("\x7f")


def _has_forbidden_ctrl(s) -> bool:
    if not isinstance(s, str):
        return False
    for ch in _FORBIDDEN_CTRL:
        if ch in s:
            return True
    return False


# ============================================================
# — agent_id 前缀强校验 (G1)
# 4 层防御: _FORBIDDEN_CTRL (现存) + 正则 + 长度 + 前缀 = node_id
# ============================================================
import re as _re_id  # noqa: E402
_NODE_ID_SEG_RE = _re_id.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_AGENT_ID_MAX_LEN = 64


def _validate_agent_id_for_node(agent_id: str, node_id: str) -> tuple[bool, str]:
    """4 层防御 + 前缀 = node_id. 返 (ok, reason)."""
    if not isinstance(agent_id, str) or not agent_id:
        return False, "empty_or_non_string"
    if len(agent_id) > _AGENT_ID_MAX_LEN:
        return False, f"too_long({len(agent_id)}>{_AGENT_ID_MAX_LEN})"
    if _has_forbidden_ctrl(agent_id):
        return False, "forbidden_ctrl_chars"
    if not isinstance(node_id, str) or not _NODE_ID_SEG_RE.match(node_id):
        return False, f"invalid_node_id:{node_id!r}"
    if not agent_id.startswith(f"{node_id}."):
        return False, f"prefix_mismatch:expected={node_id}.* got={agent_id!r}"
    return True, ""


# ============================================================
# — telemetry helpers (G2/G3/G5/G11)
# [remote-node+local-only hack 自 待 ≥3 node 升级通用 registry,
# 见 agent-gov verdict G10]
# ============================================================
_RTXDAVIS_PLUS_LOCAL_NODES = {"local", "remote-node"}  # G10 hack scope

# G2 字段白名单 (additionalProperties:false 等价, jsonschema 不引入 HC-PRE-1)
_TELEMETRY_FIELD_WHITELIST = {
    "schema_version", "kind", "ts", "node_id", "cli_type",
    "agent_id", "session_id", "model",
    "token_input", "token_output", "token_total",
    "quota_used", "quota_limit", "quota_used_pct", "quota_reset_at",
    "billing_period", "project_name", "cwd_sanitized",
    "raw_excerpt",                     # ≤2KB redacted, last_success 用
    "from_node_id",  # advisory, 必匹配 ws conn node_id (G2 SOT 校验)
    "schema_extra",  # forward-compat (G6 v1 additive non-breaking)
    "status",        # Phase A v2 (HC-DRLI-1): enum [success, fail]
}
# Phase A v2: status 加入必填 (HC-DRLI-1 显式 success/fail, 非 sticky 隐式)
_TELEMETRY_REQUIRED_FIELDS = {"schema_version", "ts", "cli_type", "status"}
_TELEMETRY_STATUS_ENUM = {"success", "fail"}
_TELEMETRY_NUMERIC_FIELDS = {
    "ts": (int, float),
    "token_input": (int,), "token_output": (int,), "token_total": (int,),
    "quota_used": (int,), "quota_limit": (int,),
    "quota_used_pct": (int, float),
}
_TELEMETRY_STRING_FIELDS = {
    "schema_version", "kind", "node_id", "cli_type", "agent_id", "session_id",
    "quota_reset_at", "billing_period", "project_name", "cwd_sanitized",
    "from_node_id",
}
_TELEMETRY_FIELD_MAX_LEN = 64        # G2 长度 cap 单字段
_TELEMETRY_PAYLOAD_MAX_BYTES = 4096  # G2 长度 cap 整 payload (4KB)
_TELEMETRY_CLI_TYPE_WHITELIST = {"claude", "codex", "gemini"}  # G8 (d) Phase A 限三家

# G5 60s 单 node ≥3 reject 触发 alert ( 限频)
_TELEMETRY_REJECT_BURST_TRACK: dict = {}  # {node_id: [ts, ts, ts]}
_TELEMETRY_REJECT_BURST_WINDOW = 60.0
_TELEMETRY_REJECT_BURST_THRESHOLD = 3

# G4 lazy stale 检测阈值 (collector_heartbeat last_seen 超 120s)
_COLLECTOR_STALE_THRESHOLD_SEC = 120.0


def _validate_telemetry_payload(node_id: str, params: dict) \
        -> tuple[bool, str, dict, dict, int]:
    """G2 4 层防御 + G3 master_post_recv 脱敏.
    返 (ok, reason, redacted_row, redact_hits, payload_size).
    """
    import json as _j
    # 步 1: payload 整体 size cap (json 序列化后)
    try:
        raw_json = _j.dumps(params, ensure_ascii=False)
    except (TypeError, ValueError):
        return False, "payload_not_serializable", {}, {}, 0
    payload_size = len(raw_json.encode("utf-8"))
    if payload_size > _TELEMETRY_PAYLOAD_MAX_BYTES:
        return False, f"payload_too_large:{payload_size}>{_TELEMETRY_PAYLOAD_MAX_BYTES}", \
               {}, {}, payload_size

    if not isinstance(params, dict):
        return False, "params_not_dict", {}, {}, payload_size

    # 步 2: 字段白名单 (additionalProperties:false)
    extra = set(params.keys()) - _TELEMETRY_FIELD_WHITELIST
    if extra:
        return False, f"unknown_fields:{sorted(extra)}", {}, {}, payload_size

    # 步 3: 必填字段
    missing = _TELEMETRY_REQUIRED_FIELDS - set(params.keys())
    if missing:
        return False, f"missing_required:{sorted(missing)}", {}, {}, payload_size

    # Phase A v2 (HC-DRLI-1): status 必显式 enum [success, fail]
    _status_val = params.get("status")
    if _status_val not in _TELEMETRY_STATUS_ENUM:
        return False, (f"bad_status_enum:{_status_val!r} "
                        f"must be one of {sorted(_TELEMETRY_STATUS_ENUM)}"), \
               {}, {}, payload_size

    # 步 4: ws conn node_id SOT 校验 (G2 from_node_id == node_id, advisory 字段)
    payload_node = params.get("from_node_id")
    if payload_node is not None and payload_node != node_id:
        return False, f"from_node_id_mismatch:claimed={payload_node!r} actual={node_id!r}", \
               {}, {}, payload_size

    # G10 Phase A hack: 限 _RTXDAVIS_PLUS_LOCAL_NODES
    if node_id not in _RTXDAVIS_PLUS_LOCAL_NODES:
        return False, f"node_not_in_phase_a:{node_id!r}", {}, {}, payload_size

    # 步 5: schema_version 必 'v1' (G6 协议 v1)
    if params.get("schema_version") != "v1":
        return False, f"unsupported_schema_version:{params.get('schema_version')!r}", \
               {}, {}, payload_size

    # 步 6: kind 必 'usage' (Phase A scope)
    kind = params.get("kind", "usage")
    if kind != "usage":
        return False, f"unsupported_kind_phase_a:{kind!r}", {}, {}, payload_size

    # 步 7: cli_type 白名单
    cli_type = params.get("cli_type")
    if cli_type not in _TELEMETRY_CLI_TYPE_WHITELIST:
        return False, f"cli_type_not_allowed:{cli_type!r}", {}, {}, payload_size

    # 步 8: 数值字段类型严校 (防 SQL 注入 / 类型混淆)
    for fname, types in _TELEMETRY_NUMERIC_FIELDS.items():
        v = params.get(fname)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, types):
            # bool 是 int 子类, 显式拒
            return False, f"bad_numeric_type:{fname}={type(v).__name__}", \
                   {}, {}, payload_size

    # 步 9: 字符串字段长度 cap + _FORBIDDEN_CTRL
    for fname in _TELEMETRY_STRING_FIELDS:
        v = params.get(fname)
        if v is None:
            continue
        if not isinstance(v, str):
            return False, f"bad_string_type:{fname}={type(v).__name__}", \
                   {}, {}, payload_size
        if len(v) > _TELEMETRY_FIELD_MAX_LEN:
            return False, f"field_too_long:{fname}={len(v)}>{_TELEMETRY_FIELD_MAX_LEN}", \
                   {}, {}, payload_size
        if _has_forbidden_ctrl(v):
            return False, f"forbidden_ctrl:{fname}", {}, {}, payload_size

    # 步 10: G3 master_post_recv 脱敏 — 命中 sensitive pattern → reject (源头 drop)
    redact_hits: dict = {}
    try:
        # 复用 的 SENSITIVE_PATTERNS 6 类
        import sys as _sys
        import os as _os
        _here_src = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _here_src not in _sys.path:
            _sys.path.insert(0, _here_src)
        from master.redact import redact as _redact
    except ImportError:
        _redact = None
    if _redact is not None:
        # 扫描所有字符串字段, 命中即 reject
        for fname in _TELEMETRY_STRING_FIELDS:
            v = params.get(fname)
            if not isinstance(v, str) or not v:
                continue
            sanitized, hits = _redact(v)
            if hits:
                # G3 sensitive 源头 drop, 不入库
                for k, c in hits.items():
                    redact_hits[k] = redact_hits.get(k, 0) + c
        if redact_hits:
            return False, f"sensitive_pattern_hit:{sorted(redact_hits.keys())}", \
                   {}, redact_hits, payload_size

    # cwd_sanitized: 若 home dir 未替换为 ~, 强制替换 (D5 G6)
    cwd = params.get("cwd_sanitized")
    if isinstance(cwd, str) and cwd:
        home = os.path.expanduser("~")
        if home and home in cwd:
            cwd = cwd.replace(home, "~")
        if cwd.startswith("/Users/") or cwd.startswith("/home/") or cwd.startswith("/root/"):
            # 路径未脱敏, 拒
            return False, "cwd_not_sanitized:home_dir_not_replaced", {}, {}, payload_size

    # 通过, 用 ws conn node_id 作 SOT 覆盖 payload (G2)
    redacted = {k: params.get(k) for k in _TELEMETRY_FIELD_WHITELIST if k in params}
    redacted["node_id"] = node_id  # SOT
    redacted.pop("from_node_id", None)  # advisory 字段, 不入库 (列里没这字段)
    if cwd:
        redacted["cwd_sanitized"] = cwd
    return True, "", redacted, redact_hits, payload_size


def _audit_telemetry(node_id: str, decision: str, reason: str,
                     payload_size: int, redact_hits: dict,
                     row_id: int = -1, from_agent_id: str = ""):
    """G11 audit log: telemetry_audit_YYYYMMDD.jsonl chmod 600 按天 rotation 30 天."""
    try:
        from pathlib import Path as _Path
        from datetime import datetime as _dt, timezone as _tz
        log_dir = _Path(os.environ.get("PRE_LOG_DIR",
                                          _PRE_LOG_ROOT)) \
                  / "security"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(log_dir), 0o700)
        except OSError:
            pass
        today = _dt.now(tz=_tz.utc).strftime("%Y%m%d")
        log_file = log_dir / f"telemetry_audit_{today}.jsonl"
        new_file = not log_file.exists()
        entry = {
            "ts": _dt.now(tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "from_node_id": node_id,
            "from_agent_id": from_agent_id,
            "decision": decision,
            "reason": reason[:200] if reason else "",
            "payload_size": payload_size,
            "redact_hits": redact_hits or {},
            "row_id": (row_id if isinstance(row_id, int) and row_id > 0
                        else (row_id if isinstance(row_id, str) and row_id else None)),
        }
        # M1 spec A: audit jsonl 全集 redact (HC-PRE-2 fail-safe)
        try:
            from master.redact import safe_audit_dump as _safe_dump
            _line = _safe_dump(entry)
        except ImportError:
            _line = json.dumps(entry, ensure_ascii=False)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(_line + "\n")
        if new_file:
            try:
                os.chmod(str(log_file), 0o600)
            except OSError:
                pass
    except OSError:
        pass


def _check_telemetry_reject_burst(node_id: str):
    """G5 60s 单 node ≥3 reject → agent-security alert (HC-T-iii 限频).
    Phase A 仅 audit 标记, 不直接 alert (alert 路径 phase 2)."""
    now = time.time()
    track = _TELEMETRY_REJECT_BURST_TRACK.setdefault(node_id, [])
    track.append(now)
    # 清理窗口外
    cutoff = now - _TELEMETRY_REJECT_BURST_WINDOW
    track[:] = [t for t in track if t >= cutoff]
    if len(track) >= _TELEMETRY_REJECT_BURST_THRESHOLD:
        _audit_telemetry(node_id, "reject_burst",
                         f"≥{_TELEMETRY_REJECT_BURST_THRESHOLD}_rejects_in_60s "
                         f"count={len(track)}",
                         0, {})


# ============================================================
# — read_pane endpoint helpers (G1-G9)
# ============================================================
from pathlib import Path  # noqa: E402

_ANSI_ESC_RE = _re_id.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[PX^_].*?\x1b\\")


def _strip_ansi(text: str) -> str:
    """G2 ANSI strip stdlib regex (HC-PRE-1, 不引 pyte/ansi2html)."""
    if not isinstance(text, str) or not text:
        return text or ""
    return _ANSI_ESC_RE.sub("", text)


# 优先 $PRE_READ_PANE_CAPABILITY (显式 path), 否则 _PRE_RULE_ROOT/hook/read_pane_capability.json
# (_PRE_RULE_ROOT 在 file 顶部 module-level 定义, env-first + __file__ sibling 推算).
_READ_PANE_CAP_PATH = Path(os.environ.get(
    "PRE_READ_PANE_CAPABILITY",
    str(Path(_PRE_RULE_ROOT) / "hook" / "read_pane_capability.json"),
))
_READ_PANE_CAP_CACHE: dict = {"mtime": 0.0, "cfg": None}


def _load_read_pane_capability() -> dict:
    """G3 mtime hot reload, fail-safe deny on error."""
    try:
        if not _READ_PANE_CAP_PATH.exists():
            return {"version": 1, "default": "deny", "allow": [], "deny": []}
        mtime = _READ_PANE_CAP_PATH.stat().st_mtime
        if _READ_PANE_CAP_CACHE["cfg"] is not None and \
                _READ_PANE_CAP_CACHE["mtime"] == mtime:
            return _READ_PANE_CAP_CACHE["cfg"]
        with open(_READ_PANE_CAP_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        _READ_PANE_CAP_CACHE["cfg"] = cfg
        _READ_PANE_CAP_CACHE["mtime"] = mtime
        return cfg
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "default": "deny", "allow": [], "deny": []}


def _check_read_pane_capability(caller_token_sha: str,
                                target_agent_id: str) -> tuple[bool, str]:
    """G3 capability check. 返 (allowed, reason)."""
    import fnmatch
    cfg = _load_read_pane_capability()
    # deny 列表优先 (黑名单)
    for entry in cfg.get("deny") or []:
        c = entry.get("caller", "*")
        t = entry.get("target", "*")
        if fnmatch.fnmatch(caller_token_sha, c) and \
                fnmatch.fnmatch(target_agent_id, t):
            return False, f"explicit_deny: {entry.get('reason', '')}"
    # allow 列表
    for entry in cfg.get("allow") or []:
        c = entry.get("caller", "*")
        t = entry.get("target", "*")
        if fnmatch.fnmatch(caller_token_sha, c) and \
                fnmatch.fnmatch(target_agent_id, t):
            return True, f"allow: {entry.get('reason', '')}"
    # default
    if cfg.get("default") == "allow":
        return True, "default_allow"
    return False, "default_deny_no_match"


_READ_PANE_RATE_TRACK: dict = {}  # {caller_token_sha: [ts, ts, ...]}
_READ_PANE_RATE_LIMIT = 1_000_000  # 本机使用, 实际不会触发
_READ_PANE_RATE_WINDOW = 60.0

# SSE 长连接限频: per-connection 而不是 per-request, 跟 _read_pane_rate_check 共享桶
# 解耦. 本机使用, 并发流上限放开至 1_000_000.
_SSE_CONN_COUNT: dict[str, int] = {}    # {caller_token_sha: active_count}
_SSE_MAX_CONN_PER_TOKEN = 1_000_000
_SSE_HEARTBEAT_SEC = 15.0               # 没数据时每 15s 发 comment ping 保活


def _read_pane_rate_check(caller_token_sha: str) -> tuple[bool, str]:
    """G9 限频 30/min per caller ( 防 capability brute force)."""
    now = time.time()
    track = _READ_PANE_RATE_TRACK.setdefault(caller_token_sha, [])
    cutoff = now - _READ_PANE_RATE_WINDOW
    track[:] = [t for t in track if t >= cutoff]
    if len(track) >= _READ_PANE_RATE_LIMIT:
        return False, f"rate_limit:{len(track)}/{_READ_PANE_RATE_LIMIT}_per_60s"
    track.append(now)
    return True, ""


def _audit_read_pane(caller_token_sha: str, target_agent_id: str,
                      target_node: str, lines_returned: int,
                      redact_hits: dict, status: str,
                      raw_disclosed: bool, decision: str, reason: str = ""):
    """G9 audit log: read_pane_audit_YYYYMMDD.jsonl chmod 600 按天 rotation 30 天."""
    try:
        from datetime import datetime as _dt, timezone as _tz
        log_dir = Path(os.environ.get(
            "PRE_LOG_DIR",
            _PRE_LOG_ROOT)) / "security"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(log_dir), 0o700)
        except OSError:
            pass
        today = _dt.now(tz=_tz.utc).strftime("%Y%m%d")
        log_file = log_dir / f"read_pane_audit_{today}.jsonl"
        new_file = not log_file.exists()
        entry = {
            "ts": _dt.now(tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "caller_token_sha": caller_token_sha,
            "target_agent_id": target_agent_id,
            "target_node": target_node,
            "lines_returned": lines_returned,
            "redact_hits": redact_hits or {},
            "status": status,
            "raw_disclosed": raw_disclosed,
            "decision": decision,
            "reason": reason[:200] if reason else "",
        }
        # M1 spec A: audit jsonl 全集 redact
        try:
            from master.redact import safe_audit_dump as _safe_dump
            _line = _safe_dump(entry)
        except ImportError:
            _line = json.dumps(entry, ensure_ascii=False)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(_line + "\n")
        if new_file:
            try:
                os.chmod(str(log_file), 0o600)
            except OSError:
                pass
    except OSError:
        pass


# G6 status enum 4 值
_READ_PANE_STATUS = {"ok", "idle", "empty", "agent_unavailable"}


# ============================================================
# transcript / agent-file endpoint helpers
# 复用 read_pane 的 capability + rate_limit 模板, audit 写独立 jsonl
# ============================================================

# transcript JSONL 单行 cap (Claude Code v2 单行最长 ~200KB, 留 1MB headroom)
_TRANSCRIPT_LINE_CAP = 1024 * 1024
# 单次返回 message 数硬上限 (UI 增量轮询场景)
_TRANSCRIPT_MAX_LIMIT = 500
# agent-file 后缀白名单 (第一版仅 markdown / 文本)
_AGENT_FILE_EXT_WHITELIST = {".md", ".markdown", ".txt"}
# agent-file 单文件 size cap (1MB; finding 文件远小于此)
_AGENT_FILE_SIZE_CAP = 1024 * 1024


def _resolve_transcript_path(cwd: str) -> str:
    """从 {pre_base_dir}/{cwd 转下划线}/transcript_path.txt 读真实 transcript JSONL 路径.

    pre/hook.py:_save_transcript_path 在 PreToolUse 持久化, 这里反向 resolve.
    返 "" 表示无 (尚未触发过 PreToolUse 或文件丢失).
    """
    if not cwd:
        return ""
    try:
        from config import load_config as _load_cfg
    except ImportError:
        return ""
    try:
        cfg = _load_cfg()
        # ensure_agent_dir 同款规则: cwd.strip('/').replace('/', '-')
        dir_name = cwd.strip("/").replace("/", "-")
        marker = os.path.join(cfg.pre_base_dir, dir_name, "transcript_path.txt")
        if not os.path.isfile(marker):
            return ""
        with open(marker, "r", encoding="utf-8") as f:
            tp = f.read().strip()
        if tp and os.path.isfile(tp):
            return tp
        return ""
    except (OSError, ValueError):
        return ""


def _normalize_transcript_msg(obj: dict) -> Optional[dict]:
    """Claude Code transcript JSONL 的单行 → UI 友好的最小 message shape.

    Claude Code 的 transcript schema (观察 v2.x): 每行一个 dict, 主要 type 包括:
      - {type: 'user', message: {role, content}}  content 可能是 str 或 [{type:'text'|'tool_result', ...}]
      - {type: 'assistant', message: {role, content}}  content 是 [{type:'text'|'tool_use', ...}]
      - {type: 'system', ...}  忽略 (subagent init / hook 回声)
      - {type: 'summary', ...}  忽略 (compaction 摘要)

    返回 {role, parts: [{kind, ...}]} 或 None (无显示价值).
    parts.kind ∈ {'text', 'tool_use', 'tool_result'}.
    """
    if not isinstance(obj, dict):
        return None
    msg_type = obj.get("type", "")
    if msg_type not in ("user", "assistant"):
        return None
    inner = obj.get("message") or {}
    role = inner.get("role") or msg_type
    content = inner.get("content")
    parts: list = []
    if isinstance(content, str):
        if content.strip():
            parts.append({"kind": "text", "text": content})
    elif isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type", "")
            if btype == "text":
                t = blk.get("text", "") or ""
                if t.strip():
                    parts.append({"kind": "text", "text": t})
            elif btype == "tool_use":
                parts.append({
                    "kind": "tool_use",
                    "tool_use_id": blk.get("id", ""),
                    "name": blk.get("name", ""),
                    "input": blk.get("input") or {},
                })
            elif btype == "tool_result":
                # content 可能是 str / list[{type:'text', text}]
                rc = blk.get("content")
                rtext = ""
                if isinstance(rc, str):
                    rtext = rc
                elif isinstance(rc, list):
                    rtext = "\n".join(
                        b.get("text", "") for b in rc
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                parts.append({
                    "kind": "tool_result",
                    "tool_use_id": blk.get("tool_use_id", ""),
                    "is_error": bool(blk.get("is_error")),
                    "text": rtext[:8192],  # cap 单 result 8KB, 防 1MB stdout 撑爆
                })
    if not parts:
        return None
    out = {
        "ts": obj.get("timestamp") or obj.get("ts") or "",
        "uuid": obj.get("uuid", ""),
        "role": role,
        "parts": parts,
    }
    return out


# 反向 paging / tail 用全文件扫描, cap 文件大小防 OOM
# transcript JSONL 单文件实际不会很大 (Claude Code v2 < 10MB 普遍), cap 50MB 安全
_TRANSCRIPT_FULL_SCAN_CAP = 50 * 1024 * 1024


def _read_transcript_window(transcript_path: str, since_byte: Optional[int] = None,
                             before_byte: Optional[int] = None,
                             tail_n: Optional[int] = None,
                             limit: int = 200) -> dict:
    """读 transcript 一个窗口, 三种模式互斥 (优先级 since > before > tail).

    返回字段:
      - messages: 解析后的消息列表, 时间序 (老 → 新)
      - next_since: forward cursor (取最后一条返回消息的 end byte)
      - prev_before: backward cursor (取第一条返回消息的 start byte; 已到头返 null)
      - eof: next_since 是否到达文件末尾
      - total_size: 当前文件 size
      - transcript_id: "{inode}:{ctime}" 用作 session 切换探测
      - reset_signal: bool, since > total_size 时 true (file 被截断/换 session)

    模式选择 (优先级):
      - since=B 且 B>0 → forward (>= B 的消息, 取前 limit 条)
      - before=B → backward (< B 的消息, 取最后 limit 条)
      - tail=N → 最末 N 条 (cap 在 limit)
      - 都没传 → 等价 tail=limit (默认全 tail)

    forward (since) 走 seek 流式读 — O(返回行); 其余走全文件扫描 (限 _TRANSCRIPT_FULL_SCAN_CAP).
    """
    out: dict = {
        "messages": [],
        "next_since": since_byte or 0,
        "prev_before": None,
        "eof": True,
        "total_size": 0,
        "transcript_id": "",
        "reset_signal": False,
    }
    try:
        st = os.stat(transcript_path)
        out["total_size"] = st.st_size
        # transcript_id = inode + ctime, session 切换 (新 transcript file) 必变
        out["transcript_id"] = f"{st.st_ino}:{int(st.st_ctime)}"

        # since 越界 → 文件被截断或换 session, 提示前端 reset
        if since_byte is not None and since_byte > st.st_size:
            out["reset_signal"] = True
            out["next_since"] = 0
            return out

        # 模式 1: forward — seek + 流式读, O(返回行)
        if since_byte is not None and since_byte > 0:
            if since_byte >= st.st_size:
                out["next_since"] = since_byte
                out["eof"] = True
                return out
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(since_byte)
                cursor = since_byte
                count = 0
                first_start = None
                while count < limit:
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        break  # 不完整行, 不前进 cursor
                    line_bytes = len(line.encode("utf-8", errors="replace"))
                    if line_bytes > _TRANSCRIPT_LINE_CAP:
                        cursor += line_bytes
                        continue
                    stripped = line.strip()
                    line_start = cursor
                    cursor += line_bytes
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                        norm = _normalize_transcript_msg(obj)
                        if norm:
                            if first_start is None:
                                first_start = line_start
                            out["messages"].append(norm)
                            count += 1
                    except (json.JSONDecodeError, ValueError):
                        continue
                out["next_since"] = cursor
                out["eof"] = (cursor >= st.st_size)
                out["prev_before"] = first_start
            return out

        # 模式 2 / 3: backward / tail — 全文件扫描
        if st.st_size > _TRANSCRIPT_FULL_SCAN_CAP:
            # 太大不扫, 退化成 forward from 0 (跟旧行为兼容)
            return _read_transcript_window(transcript_path, since_byte=0,
                                            limit=limit)

        all_msgs: list = []
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            cursor = 0
            while True:
                line = f.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    break
                line_bytes = len(line.encode("utf-8", errors="replace"))
                line_start = cursor
                cursor += line_bytes
                if line_bytes > _TRANSCRIPT_LINE_CAP:
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    norm = _normalize_transcript_msg(obj)
                    if norm:
                        norm["_start"] = line_start
                        norm["_end"] = cursor
                        all_msgs.append(norm)
                except (json.JSONDecodeError, ValueError):
                    continue

        # 选窗口
        if before_byte is not None:
            # backward: < before, 取最后 limit 条
            cands = [m for m in all_msgs if m["_start"] < before_byte]
            picked = cands[-limit:] if cands else []
            # 是否还有更早的: picked 不等于 cands 全部说明上面还有
            has_earlier = bool(picked) and len(picked) < len(cands)
            if has_earlier:
                out["prev_before"] = picked[0]["_start"]
            else:
                out["prev_before"] = None  # 已到头
        else:
            # tail (默认): 最末 N
            n = tail_n if tail_n is not None else limit
            n = min(n, limit)
            picked = all_msgs[-n:] if all_msgs else []
            if picked and len(picked) < len(all_msgs):
                out["prev_before"] = picked[0]["_start"]
            else:
                out["prev_before"] = None

        if picked:
            out["next_since"] = picked[-1]["_end"]
            out["eof"] = (out["next_since"] >= st.st_size)
        else:
            out["next_since"] = st.st_size
            out["eof"] = True

        # 清掉内部字段
        for m in picked:
            m.pop("_start", None)
            m.pop("_end", None)
        out["messages"] = picked
    except OSError:
        pass
    return out


def _list_agent_sessions(cwd: str) -> list[dict]:
    """列 agent cwd 对应的 Claude Code transcript 历史 session.

    Claude Code 把每个 session 的 jsonl 存在 ~/.claude/projects/<slug>/<uuid>.jsonl,
    /clear 不删旧文件 (新建一个), 所以这里能列到所有历史 session.

    返回按 mtime 倒序 (最新在前). 当前 active session 标 is_current=True.
    """
    current_path = _resolve_transcript_path(cwd)
    if not current_path:
        # 还没 PreToolUse 触发 → 没法定位 projects dir
        return []
    pdir = os.path.dirname(current_path)
    if not os.path.isdir(pdir):
        return []
    out = []
    for fname in os.listdir(pdir):
        if not fname.endswith(".jsonl"):
            continue
        fp = os.path.join(pdir, fname)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        session_id = fname[:-len(".jsonl")]
        # 读第一行拿 first_ts (cheap)
        first_ts = ""
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                line = f.readline().strip()
                if line:
                    obj = json.loads(line)
                    first_ts = obj.get("timestamp") or obj.get("ts") or ""
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        out.append({
            "session_id": session_id,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "first_ts": first_ts,
            "is_current": (os.path.realpath(fp) == os.path.realpath(current_path)),
        })
    out.sort(key=lambda x: -x["mtime"])
    return out


def _resolve_session_transcript(cwd: str, session_id: str) -> str:
    """把 session_id 解析成 transcript 文件 abs path. 严格校验在 projects dir 内.

    防 path traversal: session_id 必须满足 UUID-like 格式 + resolve 后在 projects dir.
    """
    import re as _re_sess
    if not session_id or not _re_sess.match(r"^[a-zA-Z0-9._\-]{1,64}$", session_id):
        return ""
    current_path = _resolve_transcript_path(cwd)
    if not current_path:
        return ""
    pdir = os.path.realpath(os.path.dirname(current_path))
    target = os.path.realpath(os.path.join(pdir, session_id + ".jsonl"))
    if not target.startswith(pdir + os.sep):
        return ""
    if not os.path.isfile(target):
        return ""
    return target


def _resolve_agent_file(cwd: str, rel_path: str) -> tuple[str, str]:
    """校验 rel_path 在 cwd 下且后缀白名单, 返 (abs_path, error).

    - rel_path 必须相对 (不以 / 开头)
    - resolve 后必须 startswith(cwd realpath) — 防 ../../ traversal + symlink 逃逸
    - 后缀必须在 _AGENT_FILE_EXT_WHITELIST
    """
    if not cwd or not rel_path:
        return "", "missing_cwd_or_path"
    if rel_path.startswith("/") or rel_path.startswith("~"):
        return "", "absolute_path_rejected"
    if "\x00" in rel_path:
        return "", "null_byte_rejected"
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in _AGENT_FILE_EXT_WHITELIST:
        return "", f"ext_not_whitelisted:{ext}"
    try:
        cwd_real = os.path.realpath(cwd)
        target = os.path.realpath(os.path.join(cwd_real, rel_path))
        if not (target == cwd_real or target.startswith(cwd_real + os.sep)):
            return "", "path_escapes_cwd"
        if not os.path.isfile(target):
            return "", "not_a_file"
        return target, ""
    except OSError as e:
        return "", f"os_error:{type(e).__name__}"


def _audit_agent_data_read(caller_token_sha: str, kind: str,
                            target_agent_id: str, target_node: str,
                            bytes_returned: int, status: str,
                            decision: str, reason: str = ""):
    """transcript / file endpoint 共用 audit log.

    log: pre_log/security/agent_data_audit_YYYYMMDD.jsonl chmod 600.
    """
    try:
        from datetime import datetime as _dt, timezone as _tz
        log_dir = Path(os.environ.get(
            "PRE_LOG_DIR",
            _PRE_LOG_ROOT)) / "security"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(log_dir), 0o700)
        except OSError:
            pass
        today = _dt.now(tz=_tz.utc).strftime("%Y%m%d")
        log_file = log_dir / f"agent_data_audit_{today}.jsonl"
        new_file = not log_file.exists()
        entry = {
            "ts": _dt.now(tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": kind,
            "caller_token_sha": caller_token_sha,
            "target_agent_id": target_agent_id,
            "target_node": target_node,
            "bytes_returned": bytes_returned,
            "status": status,
            "decision": decision,
            "reason": reason[:200] if reason else "",
        }
        try:
            from master.redact import safe_audit_dump as _safe_dump
            _line = _safe_dump(entry)
        except ImportError:
            _line = json.dumps(entry, ensure_ascii=False)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(_line + "\n")
        if new_file:
            try:
                os.chmod(str(log_file), 0o600)
            except OSError:
                pass
    except OSError:
        pass


def _sse_pack(event_name: str, data: dict) -> bytes:
    """SSE wire format: event: <name>\\ndata: <json>\\n\\n.

    EventSource 客户端按 \\n\\n 切帧, data: 行的 json 留给 JS 解析.
    """
    body = json.dumps(data, ensure_ascii=False)
    return (f"event: {event_name}\ndata: {body}\n\n").encode("utf-8")


async def _serve_transcript_sse(writer, transcript_path: str,
                                 caller_token_sha: str, agent_id: str,
                                 tail_n: int) -> None:
    """SSE 推送 transcript 增量. backfill tail N 行 + watcher 增量, 15s heartbeat.

    退出: client 断开 (ConnectionResetError / BrokenPipeError), watcher 出错,
    或 cancel. 退出前 watcher.unsubscribe + writer.close. 不抛异常给上游 (handle_http
    会被吞掉 finally 里的 audit 不出).
    """
    from master import transcript_watcher as _tw
    # SSE 响应头 — 不带 Content-Length, HTTP/1.1 视为 "读到 connection close" 为止.
    header = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream; charset=utf-8\r\n"
        "Cache-Control: no-cache, no-transform\r\n"
        "X-Accel-Buffering: no\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        writer.write(header)
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        return

    # 1) backfill: 读最后 N 行 (复用 _read_transcript_window tail 模式)
    backfill = _read_transcript_window(transcript_path, tail_n=tail_n)
    backfill_evt = {
        "transcript_id": backfill.get("transcript_id", ""),
        "messages": backfill.get("messages", []),
        "next_since": backfill.get("next_since", 0),
        "total_size": backfill.get("total_size", 0),
        "eof": backfill.get("eof", True),
    }
    try:
        writer.write(_sse_pack("backfill", backfill_evt))
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        return

    # 2) attach watcher; offset 对齐 backfill 末尾, 避免重发
    try:
        watcher = await _tw.get_or_create(transcript_path, _normalize_transcript_msg)
        # 第一个订阅者会自动 sync_to_eof; 多订阅者共享 offset, 这里再显式对齐一次
        next_since = int(backfill.get("next_since") or 0)
        if next_since and next_since > watcher.offset:
            watcher.offset = next_since
            watcher.transcript_id = backfill.get("transcript_id", "") or watcher.transcript_id
        init_state, q = await watcher.subscribe(sync_to_eof=False)
    except Exception as e:
        # watcher 创建失败 — 给客户端发一次 error 然后关
        try:
            writer.write(_sse_pack("error",
                                     {"reason": "watcher_init_failed",
                                      "detail": f"{type(e).__name__}: {e}"[:200]}))
            await writer.drain()
        finally:
            try:
                writer.close()
            except Exception:
                pass
        return

    # 3) 推送循环: 队列等增量, 超过 heartbeat 间隔发 comment ping
    last_send = time.time()
    try:
        # 先吐 ready (UI 用来转 "sending → sent" 时机也可参考)
        writer.write(_sse_pack("ready", init_state))
        await writer.drain()
        while True:
            timeout = max(0.5, _SSE_HEARTBEAT_SEC - (time.time() - last_send))
            try:
                evt = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                # 心跳 — SSE 注释行 (": ...\n\n"), 客户端忽略, 仅用来保活
                writer.write(b": ping\n\n")
                await writer.drain()
                last_send = time.time()
                continue
            event_name = evt.get("event", "message")
            writer.write(_sse_pack(event_name, evt))
            await writer.drain()
            last_send = time.time()
            if event_name == "lagged":
                # 慢消费者已被 watcher 踢出, 这里收完 lagged 也关流, 让客户端重连
                break
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception:
        # 兜底: 流内异常不外抛
        pass
    finally:
        try:
            watcher.unsubscribe(q)
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass


def _check_collector_stale(node_id: str, registry):
    """G4 lazy stale 检测: collector_heartbeat last_seen > 120s → audit + finding.
    Phase A 仅 audit + finding 文件, alert 路径 phase 2."""
    last_seen_map = getattr(registry, "collector_last_seen", None) or {}
    last = last_seen_map.get(node_id)
    if last is None:
        return  # 无 heartbeat 历史, skip (首次 report 可能先于 heartbeat)
    age = time.time() - last
    if age > _COLLECTOR_STALE_THRESHOLD_SEC:
        _audit_telemetry(node_id, "stale_warning",
                         f"collector_heartbeat_age={age:.1f}s>{_COLLECTOR_STALE_THRESHOLD_SEC}s",
                         0, {})


def _required_role_for_path(method: str, path: str, is_ws_upgrade: bool) -> tuple[Optional[str], str]:
    """根据 URL 推 (required_role, required_scope). 第一期粗颗 4 role.

    返 (role, scope) — role=None 表示 任何已登记 role 都行.
    scope="" 表示 仅校 token 有效, 不查具体 scope.
    """
    # WS /node 接入: 仅 node role
    if is_ws_upgrade and path == "/node":
        return "node", "bus.connect"

    # admin / token 管理: 仅 operator
    if path.startswith("/api/v1/admin") or "/tokens" in path:
        return "operator", "admin.tokens"

    # agent 控制 (改 mode / kill / 命令): 仅 operator
    if "/agents/" in path and (path.endswith("/mode") or path.endswith("/kill")
                                 or path.endswith("/control")):
        return "operator", "agent.control"

    # SSE ticket 颁发: 任何 role, 要 bus.pane.read scope (与 /transcript 一致)
    if path == "/api/v1/auth/sse-ticket":
        return None, "bus.pane.read"

    # 各 agent 资源访问: 任何 role (mcp/cli/operator/node) 都 ok, 但要求 token 有 scope
    if "/agents/" in path:
        if path.endswith("/send"):
            return None, "bus.message.send"
        if "/messages" in path:
            return None, "bus.message.fetch"
        if "/pane" in path:
            return None, "bus.pane.read"
        if path.endswith("/transcript"):
            return None, "bus.pane.read"
        if path.endswith("/transcript/stream"):
            # SSE 用 ticket 自验, _check_auth 已早 return; 这条不会被命中 (兜底)
            return None, "bus.pane.read"
        if path.endswith("/file"):
            return None, "bus.pane.read"
        if path.endswith("/sessions"):
            return None, "bus.pane.read"
        if "/cycle_state" in path:
            return None, "bus.cycle_state"

    # 其它 /api/v1/* 默认: 仅校 token 有效, 不查 scope (向后兼容现有 endpoint)
    return None, ""


# ---------- PR2: caller 来源差异化校验 ----------
# 按 (role, path, source IP) 三元组校验, audit 全部 caller, 异常组合写 finding.
# 当前阶段策略软优先: mcp/hook role + 非 loopback 硬拒, 其他组合软审计.
# PR3-4 caller 切换完后, 可在 _classify_caller 内收紧白名单 (e.g. hook 限 send/messages).

def _classify_caller(role: str, path: str, source_ip: str,
                      is_ws_upgrade: bool) -> tuple[bool, str, str]:
    """按 (role, path, source IP) 三元组校验 caller 来源.

    返 (allowed, deny_reason, caller_class_label).
    caller_class_label 形如 "mcp@loopback" / "hook@10.0.0.5" 等, 进 audit 字段.
    """
    is_loopback = source_ip in ("", "127.0.0.1", "::1", "localhost")
    if role == "mcp" and not is_loopback:
        return False, f"mcp_role_remote_ip_denied:{source_ip}", f"mcp@{source_ip}"
    if role == "hook" and not is_loopback:
        return False, f"hook_role_remote_ip_denied:{source_ip}", f"hook@{source_ip}"
    src_label = "loopback" if is_loopback else source_ip
    return True, "ok", f"{role}@{src_label}"


_CALLER_AUDIT_RATE_WINDOWS: dict[str, list[float]] = {}
_CALLER_AUDIT_LIMIT_PER_MIN = 60  # 单 caller_class 60/min, 防 audit spam


def _audit_caller_class(caller_class: str, role: str, path: str, method: str,
                          source_ip: str, decision: str, reason: str,
                          token_label: str = ""):
    """append pre_log/security/caller_class_audit_YYYYMMDD.jsonl, chmod 600.
    单 caller_class 60/min 限频 (跟 _audit_rate_check 同思路, 防 spam).
    """
    try:
        now = time.time()
        window_start = now - 60.0
        arr = _CALLER_AUDIT_RATE_WINDOWS.setdefault(caller_class, [])
        arr[:] = [t for t in arr if t > window_start]
        if len(arr) >= _CALLER_AUDIT_LIMIT_PER_MIN:
            return
        arr.append(now)

        from pathlib import Path as _Path
        from datetime import datetime as _dt, timezone as _tz
        log_dir = _Path(os.environ.get("PRE_LOG_DIR",
                                          _PRE_LOG_ROOT)) \
                  / "security"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(log_dir), 0o700)
        except OSError:
            pass
        today = _dt.now(tz=_tz.utc).strftime("%Y%m%d")
        log_file = log_dir / f"caller_class_audit_{today}.jsonl"
        new_file = not log_file.exists()
        entry = {
            "ts": _dt.now(tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "caller_class": caller_class,
            "role": role,
            "token_label": token_label,
            "source_ip": source_ip,
            "method": method,
            "path": path[:200],
            "decision": decision,
            "reason": reason[:200] if reason else "",
        }
        try:
            from master.redact import safe_audit_dump as _safe_dump
            line = _safe_dump(entry)
        except ImportError:
            line = json.dumps(entry, ensure_ascii=False)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if new_file:
            try:
                os.chmod(str(log_file), 0o600)
            except OSError:
                pass
    except OSError:
        pass


def _write_caller_class_finding(caller_class: str, role: str, path: str,
                                  source_ip: str, reason: str):
    """异常 caller class → WARNING finding. ts 后缀防 dup spam."""
    try:
        from pathlib import Path as _Path
        findings = _Path(_PRE_LOG_ROOT) / "findings"
        findings.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        fpath = findings / f"WARNING-master-caller-class-anomaly-{ts}.md"
        body = (
            f"# WARNING: master caller class anomaly\n\n"
            f"- ts: {ts}\n"
            f"- caller_class: {caller_class}\n"
            f"- role: {role}\n"
            f"- source_ip: {source_ip}\n"
            f"- path: {path}\n"
            f"- reason: {reason}\n\n"
            f"## context\n\n"
            f"mcp/hook role 必须经 loopback. 非 loopback IP 直接拒.\n"
            f"合法跨机调用须用 ssh tunnel + node role 的 ws 通路.\n"
        )
        fpath.write_text(body, encoding="utf-8")
        try:
            os.chmod(str(fpath), 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _check_auth(method: str, path: str, headers: dict, query: dict,
                db, body: bytes = b"",
                source_ip: str = "") -> tuple[bool, str, dict]:
    """
    HTTP/WS 共用 auth check (multi-token RBAC).

    返 (ok, reason, ctx). ctx 命中时为 {label, role, scopes, agent_id}.

    GET /healthz 和 / 不要 auth.
    其他: Authorization: Bearer <token> 或 ?token=<token>, token 在 bus_tokens 表内.
    POST + WS Upgrade: 还要 Origin 白名单 (无 Origin 视为 CLI, 通过).
    mcp role 的 token 携带 from_agent 字段时, 必等于 token 绑定的 agent_id.
    """
    from master.auth import verify_token, extract_bearer

    public_paths = ("/", "/healthz")
    is_upgrade = headers.get("upgrade", "").lower() == "websocket"

    if method == "GET" and path in public_paths and not is_upgrade:
        return True, "public", {}

    # SSE stream: EventSource 无法带 Authorization header, 走 ?ticket=<x>.
    # ticket 由 POST /api/v1/auth/sse-ticket 颁发 (走 Bearer scope 检查), TTL 8min.
    # 这里 peek 不消费 — EventSource 重连可在 TTL 内复用同一 ticket.
    if (method == "GET" and "/api/v1/agents/" in path
            and path.endswith("/transcript/stream")):
        from master import sse_ticket
        agent_id = path[len("/api/v1/agents/"):-len("/transcript/stream")]
        tk_ctx = sse_ticket.peek(query.get("ticket", ""), agent_id)
        if not tk_ctx:
            return False, "invalid_or_expired_ticket", {}
        return True, "ok_ticket", {
            "caller_token_sha": tk_ctx["caller_token_sha"],
            "agent_id": agent_id,
            "auth_mode": "ticket",
            # role/label 仅用于 _classify_caller 与 audit; ticket 是 gui-only 颁发的, 标 gui.
            "role": "gui",
            "label": "<sse-ticket>",
            "scopes": ["bus.pane.read"],
        }

    # 提 Bearer (header > query)
    token = extract_bearer(headers.get("authorization", ""))
    if not token:
        token = query.get("token", "")
    if not token:
        return False, "missing_or_bad_bearer", {}

    # required role + scope
    expected_role, required_scope = _required_role_for_path(method, path, is_upgrade)

    # mcp from_agent 绑定: POST /agents/.../send 携带 from_agent 字段时要校
    from_agent_check: Optional[str] = None
    if path.endswith("/send") and method == "POST" and body:
        try:
            payload_doc = json.loads(body.decode("utf-8"))
            fa = payload_doc.get("from_agent")
            if isinstance(fa, str) and fa:
                from_agent_check = fa
        except (ValueError, UnicodeDecodeError):
            pass

    ok, reason, ctx = verify_token(
        db, token,
        required_scope=required_scope,
        expected_role=expected_role,
        from_agent=from_agent_check,
    )
    if not ok:
        return False, reason, {}

    # PR2: caller 来源差异化校验 (role + source IP) + audit
    role = ctx.get("role", "")
    token_label = ctx.get("label", "")
    classify_ok, classify_reason, caller_class = _classify_caller(
        role, path, source_ip, is_upgrade,
    )
    _audit_caller_class(
        caller_class=caller_class, role=role, path=path, method=method,
        source_ip=source_ip,
        decision="allow" if classify_ok else "deny",
        reason=classify_reason,
        token_label=token_label,
    )
    if not classify_ok:
        _write_caller_class_finding(caller_class, role, path, source_ip,
                                     classify_reason)
        return False, classify_reason, {}

    # Origin (POST + Upgrade)
    if method == "POST" or is_upgrade:
        origin = headers.get("origin", "")
        if origin and origin not in ORIGIN_WHITELIST:
            return False, f"origin_not_allowed:{origin}", ctx

    return True, "ok", ctx


# ---------- prehook 决策日志 ----------
# _PRE_ROOT / _PRE_RULE_ROOT / _PRE_LOG_ROOT 在 file 顶部 module-level 定义 (single source).

PREHOOK_LOG_DIR = os.path.join(_PRE_RULE_ROOT, "logs")

# 跨 node 文件交换存储路径
FILES_DIR = os.path.join(_PRE_LOG_ROOT, "files")
FILES_AUDIT_DIR = FILES_DIR  # audit jsonl 跟 file 同目录
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB
FILE_RETENTION_DAYS = 30
# in-memory ACL: file_id → {owner, recipient, ts, name, size, sha256}
_FILE_META: dict[str, dict] = {}
# 限频: (agent_id, op) → [ts]
_FILE_RATE_WINDOWS: dict[tuple, list[float]] = {}
FILE_RATE_LIMITS = {
    "upload": {"per_agent_per_hour": 1_000_000},
    "download": {"per_agent_per_hour": 1_000_000},
}


def _file_rate_check(agent_id: str, op: str) -> tuple[bool, str]:
    """sliding window 1h 限频. 返 (allowed, reason)."""
    if op not in FILE_RATE_LIMITS:
        return True, ""
    limit = FILE_RATE_LIMITS[op]["per_agent_per_hour"]
    now = time.time()
    window_start = now - 3600.0
    key = (agent_id, op)
    arr = _FILE_RATE_WINDOWS.setdefault(key, [])
    arr[:] = [t for t in arr if t > window_start]
    if len(arr) >= limit:
        return False, f"rate_limited:{op} {len(arr)}/h >= {limit}"
    arr.append(now)
    return True, ""


# phase 1: GET /api/v1/notify/audit 限频, 本机使用上调至 1_000_000.
# (保留 sliding-window 框架, last-success endpoint 仍复用)
_AUDIT_RATE_WINDOWS: dict[str, list[float]] = {}
_AUDIT_RATE_LIMIT_PER_MIN = 1_000_000


def _audit_rate_check(bearer_key: str) -> tuple[bool, str]:
    """sliding window 60s 限频 30/min. 返 (allowed, reason).
    bearer_key: Bearer token 的 sha256[:12], 作 per-Bearer key (单 user 模型下=global).
    """
    now = time.time()
    window_start = now - 60.0
    arr = _AUDIT_RATE_WINDOWS.setdefault(bearer_key, [])
    arr[:] = [t for t in arr if t > window_start]
    if len(arr) >= _AUDIT_RATE_LIMIT_PER_MIN:
        return False, f"rate_limited_audit {len(arr)}/min >= {_AUDIT_RATE_LIMIT_PER_MIN}"
    arr.append(now)
    return True, ""


def _sanitize_name(name: str) -> str:
    """file name → safe chars [a-zA-Z0-9._-], 限 80 字"""
    import re as _re
    s = _re.sub(r"[^a-zA-Z0-9._-]", "_", name or "unnamed")
    return s[:80] or "unnamed"


def _file_path(agent_id: str, file_id: str, name: str) -> str:
    safe_aid = _sanitize_name(agent_id)
    safe_name = _sanitize_name(name)
    agent_dir = os.path.join(FILES_DIR, safe_aid)
    os.makedirs(agent_dir, exist_ok=True)
    try:
        os.chmod(agent_dir, 0o700)
    except OSError:
        pass
    return os.path.join(agent_dir, f"{file_id}_{safe_name}")


def _audit_file(entry: dict):
    """append pre_log/files/file_audit_YYYYMMDD.jsonl"""
    try:
        os.makedirs(FILES_AUDIT_DIR, exist_ok=True)
        from datetime import datetime as _dt, timezone as _tz
        date_str = _dt.now(_tz.utc).strftime("%Y%m%d")
        log_file = os.path.join(FILES_AUDIT_DIR, f"file_audit_{date_str}.jsonl")
        new = not os.path.exists(log_file)
        # M1 spec A: audit jsonl 全集 redact
        try:
            from master.redact import safe_audit_dump as _safe_dump
            _line = _safe_dump(entry)
        except ImportError:
            _line = json.dumps(entry, ensure_ascii=False)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(_line + "\n")
        if new:
            try:
                os.chmod(log_file, 0o600)
            except OSError:
                pass
    except OSError:
        pass


def rotate_old_files(days_keep: int = 30):
    """删 pre_log/files/<agent>/* mtime > N 天 + audit jsonl > N 天."""
    if not os.path.isdir(FILES_DIR):
        return
    cutoff = time.time() - days_keep * 86400
    for root, _, files in os.walk(FILES_DIR):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if os.stat(fp).st_mtime < cutoff:
                    os.unlink(fp)
            except OSError:
                continue


def _summarize_prehook_input(tool: str, input_dict: dict) -> str:
    """根据 tool 类型抽 input_preview (首选具体字段, 兜底 dump)."""
    if not isinstance(input_dict, dict):
        return ""
    if tool == "Bash":
        cmd = input_dict.get("command", "") or ""
        return cmd[:150]
    if tool in ("Read", "Write", "Edit"):
        return input_dict.get("file_path", "") or ""
    if tool in ("Grep", "Glob"):
        return input_dict.get("pattern", "") or ""
    if tool == "Agent":
        return input_dict.get("description", "") or ""
    # 兜底: command_preview / 任意短字符串字段
    for k in ("command_preview", "description", "command", "file_path", "pattern"):
        v = input_dict.get(k)
        if isinstance(v, str) and v:
            return v[:150]
    try:
        return json.dumps(input_dict, ensure_ascii=False)[:150]
    except Exception:
        return ""


def load_recent_prehook_decisions(cwd: str, limit: int = 5) -> list[dict]:
    """读当天 jsonl, 倒序过滤 cwd 匹配的 decision 条目, 取前 limit 条.
    GUI 决策卡显示该 agent 最近 prehook 评价."""
    from datetime import datetime, timezone
    if not cwd:
        return []
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = os.path.join(PREHOOK_LOG_DIR, f"pre_hook_{date_str}.jsonl")
    if not os.path.exists(log_file):
        return []
    out = []
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    # 倒序扫
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("cwd") != cwd:
            continue
        if "decision" not in ev:
            # stop event 等无 decision 字段, 跳过
            continue
        out.append({
            "ts": ev.get("ts", ""),
            "tool": ev.get("tool", ""),
            "decision": ev.get("decision", ""),
            "reason": ev.get("reason", ""),
            "source": ev.get("source", ""),
            "input_preview": _summarize_prehook_input(ev.get("tool", ""),
                                                     ev.get("input", {})),
        })
        if len(out) >= limit:
            break
    return out


def find_active_prehook_for_pending(cwd: str, since_pane_ts: float | None) -> Optional[dict]:
    """关联 active pending UI 对应的 prehook entry.
    倒序扫当天 jsonl, 找 cwd 匹配 + decision='ask' 的最近一条;
    若 since_pane_ts 已知, 仅当该 entry ts ∈ [since_pane_ts - 600, since_pane_ts + 60] 才算 active
    (容差: hook 决定 ask 一般在 driver 看到 pending 前 < 1 分钟, 多给几分钟兜底).
    返 {decision, reason, source, ts, tool} 或 None."""
    from datetime import datetime, timezone
    if not cwd:
        return None
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = os.path.join(PREHOOK_LOG_DIR, f"pre_hook_{date_str}.jsonl")
    if not os.path.exists(log_file):
        return None
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("cwd") != cwd:
            continue
        if ev.get("decision") != "ask":
            continue
        # ts 窗口校验
        if since_pane_ts is not None:
            try:
                ev_ts = datetime.fromisoformat(
                    ev["ts"].replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, KeyError):
                ev_ts = None
            if ev_ts is not None and not (since_pane_ts - 600 <= ev_ts <= since_pane_ts + 60):
                # 历史 ask, 不算当前 pending 的
                continue
        return {
            "ts": ev.get("ts", ""),
            "tool": ev.get("tool", ""),
            "decision": ev.get("decision", ""),
            "reason": ev.get("reason", ""),
            "source": ev.get("source", ""),
        }
    return None


# ---------- HTTP handler ----------

async def trigger_node_rediscover(node_id: str, registry) -> tuple[bool, dict]:
    """让 node 重新 discover_agents 并 re-register. fire-and-forget."""
    node = registry.get_node(node_id)
    if not node or not node.ws_writer:
        return False, {"reason": "node_not_found", "node_id": node_id}
    rpc = {"jsonrpc": "2.0", "method": "discover_agents", "params": {}}
    try:
        await send_to_writer(node.ws_writer, json.dumps(rpc, ensure_ascii=False))
    except Exception as e:
        return False, {"reason": "ws_send_failed", "detail": str(e)}
    return True, {"node_id": node_id, "queued": True}


async def forward_decide_to_agent(agent_id: str, key: str, registry, db,
                                   by_agent: str = "master.api") -> tuple[bool, dict]:
    """远程注入按键给 agent 的 ask UI. 留 audit 日志到 messages 表."""
    import uuid
    if key not in DECIDE_KEY_WHITELIST:
        return False, {"reason": "key_not_allowed", "key": key,
                       "allowed": sorted(DECIDE_KEY_WHITELIST)}
    a = registry.get_agent(agent_id)
    if not a:
        return False, {"reason": "agent_not_found", "agent_id": agent_id}
    node = registry.get_node(a.node_id)
    if not node or not node.ws_writer:
        return False, {"reason": "node_offline", "node_id": a.node_id}

    decide_id = uuid.uuid4().hex
    # audit log: 一条 kind=decide 的 message, payload 含 key + by
    audit = {
        "id": decide_id, "ts": time.time(),
        "from_agent": by_agent, "to_agent": agent_id,
        "from_role": "operator", "to_role": a.role,
        "kind": "decide", "payload": {"key": key},
        "parent_id": None, "priority": 0,
    }
    try:
        db.insert_message(audit)
    except Exception:
        pass

    rpc = {
        "jsonrpc": "2.0", "method": "command_agent",
        "params": {
            "agent_id": agent_id, "driver_type": a.driver_type,
            "op": "decide", "args": {"key": key},
        },
    }
    try:
        await send_to_writer(node.ws_writer, json.dumps(rpc, ensure_ascii=False))
    except Exception as e:
        return False, {"reason": "ws_send_failed", "detail": str(e)}
    # 登记 pending entry, pane_fp 待第一次心跳 baseline.
    # 不要回执, 重试由 report_activity 心跳触发, 按 pane_fp 字节指纹判定.
    now = time.time()
    _PENDING_DECIDES[agent_id] = {
        "key": key,
        "decide_id": decide_id,
        "by_agent": by_agent,
        "first_ts": now,
        "last_try_ts": now,
        "tries": 1,
        "node_id": a.node_id,
        "driver_type": a.driver_type,
        "pane_fp": None,  # 等下一次心跳取 baseline
    }
    return True, {"decide_id": decide_id, "node_id": a.node_id, "key": key, "tries": 1}


async def _resend_pending_decide(agent_id: str, registry) -> bool:
    """重发已登记的 decide. 不动 pane_fp baseline, 不写新 audit."""
    entry = _PENDING_DECIDES.get(agent_id)
    if not entry:
        return False
    node = registry.get_node(entry["node_id"])
    if not node or not node.ws_writer:
        return False
    rpc = {
        "jsonrpc": "2.0", "method": "command_agent",
        "params": {
            "agent_id": agent_id, "driver_type": entry["driver_type"],
            "op": "decide", "args": {"key": entry["key"]},
        },
    }
    try:
        await send_to_writer(node.ws_writer, json.dumps(rpc, ensure_ascii=False))
    except Exception as e:
        print(f"[master] decide retry ws_send_failed agent={agent_id}: {e}", flush=True)
        return False
    entry["last_try_ts"] = time.time()
    entry["tries"] += 1
    print(f"[master] decide retry agent={agent_id} key={entry['key']} "
          f"tries={entry['tries']}/{_PENDING_DECIDE_MAX_TRIES} "
          f"baseline_fp={entry['pane_fp'][:8] if entry['pane_fp'] else 'NONE'}",
          flush=True)
    return True


async def _process_pending_decides_on_activity(activity_list: list[dict], registry):
    """
    report_activity 心跳触发的 decide 重试 / 弹出.
    判据按 pane_fp 字节指纹, 不靠 state (state 滞后会误判).
    - first_ts 老于 MAX_AGE → 弹出
    - 首次心跳 (entry.pane_fp is None) → cur_fp 当 baseline, 不重发
    - state != blocked_user → 视为成功, 弹出
    - cur_fp != entry.pane_fp → pane 已变, 视为按键已消化, 弹出
    - state == blocked_user 且 cur_fp == entry.pane_fp → 重发 (限 MAX_TRIES)
    """
    if not _PENDING_DECIDES:
        return
    now = time.time()
    seen_ids = set()
    for a in activity_list:
        aid = a.get("agent_id")
        if not aid:
            continue
        seen_ids.add(aid)
        entry = _PENDING_DECIDES.get(aid)
        if not entry:
            continue
        cur_fp = a.get("pane_fp")
        state = a.get("state", "")
        # 寿命到顶强制弹
        if now - entry["first_ts"] > _PENDING_DECIDE_MAX_AGE:
            print(f"[master] decide give_up agent={aid} key={entry['key']} "
                  f"reason=max_age age={now - entry['first_ts']:.1f}s "
                  f"tries={entry['tries']}", flush=True)
            _PENDING_DECIDES.pop(aid, None)
            continue
        # 首次心跳 → baseline
        if entry["pane_fp"] is None:
            if cur_fp:
                entry["pane_fp"] = cur_fp
                print(f"[master] decide baseline_set agent={aid} key={entry['key']} "
                      f"fp={cur_fp[:8]} state={state}", flush=True)
            continue
        # state 已不在 blocked_user → 成功
        if state != "blocked_user":
            print(f"[master] decide cleared agent={aid} key={entry['key']} "
                  f"reason=state={state} tries={entry['tries']}", flush=True)
            _PENDING_DECIDES.pop(aid, None)
            continue
        # pane 字节级已变 → ask 区已消化, 不再重发
        if cur_fp and cur_fp != entry["pane_fp"]:
            print(f"[master] decide cleared agent={aid} key={entry['key']} "
                  f"reason=pane_changed old={entry['pane_fp'][:8]} "
                  f"new={cur_fp[:8]} tries={entry['tries']}", flush=True)
            _PENDING_DECIDES.pop(aid, None)
            continue
        # state==blocked_user 且 pane_fp 字节级未变 → 重发
        if entry["tries"] >= _PENDING_DECIDE_MAX_TRIES:
            print(f"[master] decide give_up agent={aid} key={entry['key']} "
                  f"reason=max_tries tries={entry['tries']}", flush=True)
            _PENDING_DECIDES.pop(aid, None)
            continue
        await _resend_pending_decide(aid, registry)
    # 孤儿 entry (本次心跳没有对应 activity) 老的弹掉
    for aid in list(_PENDING_DECIDES.keys()):
        if aid in seen_ids:
            continue
        entry = _PENDING_DECIDES[aid]
        if now - entry["first_ts"] > _PENDING_DECIDE_MAX_AGE:
            print(f"[master] decide drop_orphan agent={aid} key={entry['key']} "
                  f"age={now - entry['first_ts']:.1f}s", flush=True)
            _PENDING_DECIDES.pop(aid, None)


async def forward_send_to_agent(agent_id: str, body: dict, registry, db) -> tuple[bool, dict]:
    """
    HTTP POST /api/v1/agents/{id}/send → 路由到对应 node 的 WS, fire-and-forget.
    body: {"kind":"command", "payload":{"text":...}, "priority":0?, "parent_id":null?}
    """
    import uuid

    # F: kind 白名单
    kind = body.get("kind", "command")
    if kind not in SEND_KIND_WHITELIST:
        return False, {"reason": "kind_not_allowed", "kind": kind,
                       "allowed": sorted(SEND_KIND_WHITELIST)}

    # M7-3: mcp_tool_call kind 时 master 二次校 from_agent prefix == 转发 source node
    # defense-in-depth (mcp_server 端 M7-2 已校 caller, 这是 master 侧再校防止 spoof)
    if kind == "mcp_tool_call":
        _from = body.get("from_agent", "")
        _src = body.get("_validated_source_node", "")
        if not _from or '.' not in _from or not _src:
            return False, {"reason": "M7-3_mcp_tool_call_missing_from_or_source",
                           "from_agent": _from, "source_node": _src}
        if _from.split('.')[0] != _src:
            # finding HIGH-master-from-agent-spoof (跟 D4 一致)
            try:
                import json as _j
                from datetime import datetime as _dt, timezone as _tz
                from pathlib import Path as _Path
                _findings = _Path(_PRE_LOG_ROOT) / "findings"
                _findings.mkdir(parents=True, exist_ok=True)
                _ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
                _fpath = _findings / f"HIGH-master-from-agent-spoof-{_ts}.md"
                with open(_fpath, "w") as _f:
                    _f.write(
                        f"# HIGH: master from_agent spoof attempt\n\n"
                        f"- ts: {_ts}\n"
                        f"- kind: mcp_tool_call\n"
                        f"- claimed from_agent: {_from}\n"
                        f"- validated source node (X-FN-Node-Id): {_src}\n"
                        f"- prefix mismatch: {_from.split('.')[0]} != {_src}\n"
                        f"- ADR: D4 M7-3\n\n"
                        f"<agent-security M7-3 defense-in-depth>\n"
                    )
                try:
                    os.chmod(str(_fpath), 0o600)
                except OSError:
                    pass
            except OSError:
                pass
            return False, {"reason": "M7-3_from_agent_prefix_spoof",
                           "from_agent": _from, "source_node": _src,
                           "expected_prefix": _src}

    # E: payload 净化
    payload = body.get("payload", {}) or {}
    for field in ("text", "prompt", "comment", "summary"):
        v = payload.get(field)
        if isinstance(v, str) and _has_forbidden_ctrl(v):
            return False, {"reason": "payload_has_control_chars", "field": field}

    # M1 spec A (HIGH 优先级 agent-security): bus_send payload 入库前 redact.
    # 替换 SENSITIVE_PATTERNS 7 类 (AWS key/Bearer/sk_*/UUID/private key/SSH path/OAuth code).
    # 入库后 master.db / audit / 跨 node 转发都不含 raw 敏感数据, 仅留 placeholder.
    # 仅 top-level string 字段 (避免破嵌套结构), 不修改非字符串字段.
    redact_hits_send: dict = {}
    try:
        from master.redact import redact as _redact_send
        _redacted = dict(payload)
        for _k, _v in payload.items():
            if isinstance(_v, str) and _v:
                _s, _h = _redact_send(_v)
                if _s != _v:
                    _redacted[_k] = _s
                    for _hk, _hc in _h.items():
                        redact_hits_send[_hk] = redact_hits_send.get(_hk, 0) + _hc
        payload = _redacted
        body = {**body, "payload": payload}  # 后续 body.get("payload") 也用 redacted
    except ImportError:
        pass  # fail-safe: redact 不可用 → 不 redact 但不阻 send
    # command/chat/evaluate_request: text 必填
    if kind in ("command", "chat", "evaluate_request"):
        if not (payload.get("text") or payload.get("prompt") or payload.get("task")):
            return False, {"reason": "missing_payload_text_or_task", "kind": kind}
    # 派单类: dispatch_id 必填
    if kind in ("verdict_reply", "task_verdict", "task_request", "evaluate_request") \
            and not payload.get("dispatch_id"):
        return False, {"reason": "missing_payload_dispatch_id", "kind": kind}

    # virtual agent (user.default 等) 短路 — 不走 ws node, 调 notify_abstract
    if agent_id in VIRTUAL_AGENTS:
        from_agent = body.get("from_agent", "master.api")
        priority = (payload.get("priority") or "normal").lower()
        # M2 + : priority 严格白名单, 不在 → 降级 normal + audit invalid
        invalid_priority = False
        if priority not in PRIORITY_WHITELIST:
            invalid_priority = priority
            priority = "normal"
        # M3 + : 限频 sliding window
        allowed, rate_reason = _rate_check(from_agent, priority)
        msg_id = uuid.uuid4().hex
        msg_dict = {
            "id": msg_id, "ts": time.time(),
            "from_agent": from_agent, "to_agent": agent_id,
            "from_role": body.get("from_role", "agent"),
            "to_role": "user",
            "kind": kind,
            "payload": {**payload, "priority": priority,
                        "_rate_blocked": not allowed,
                        "_invalid_priority_input": invalid_priority,
                        "_critical_burst_capped": (kind != "alert"
                                                   and invalid_priority is False
                                                   and not allowed
                                                   and priority == "critical")},
            "parent_id": body.get("parent_id"),
            "priority": body.get("priority", 0),
        }
        try:
            db.insert_message(msg_dict)
        except Exception as e:
            print(f"[master] virtual agent insert failed: {e}", flush=True)
        if not allowed:
            return True, {"msg_id": msg_id, "audit_only": True,
                          "rate_limited": True, "reason": rate_reason,
                          "to_agent": agent_id}
        # 调 notify_abstract.send_all (fire-and-forget, HC-PRE-2 + )
        text = payload.get("text") or payload.get("prompt") or ""
        async def _bg_notify():
            try:
                from master.notify_abstract import send_all
                await send_all(text=text, priority=priority,
                               payload=payload, agent_from=from_agent,
                               to_user=agent_id)
            except Exception as e:
                print(f"[master] notify_abstract.send_all failed: {e}", flush=True)
        import asyncio as _asyncio
        _asyncio.create_task(_bg_notify())
        return True, {"msg_id": msg_id, "audit_only": True,
                      "to_user": agent_id, "priority": priority,
                      "invalid_priority_downgraded": bool(invalid_priority)}

    # mcp_tool_call 永远 audit_only — 不查 registry, 不 forward
    # to_agent 通常是 audit.mcp placeholder, mcp_server 子进程是终点
    if kind == "mcp_tool_call":
        msg_id = uuid.uuid4().hex
        msg_dict = {
            "id": msg_id, "ts": time.time(),
            "from_agent": body.get("from_agent", "unknown.mcp"),
            "to_agent": agent_id,
            "from_role": body.get("from_role", "agent"),
            "to_role": "audit",
            "kind": "mcp_tool_call",
            "payload": payload,
            "parent_id": body.get("parent_id"),
            "priority": body.get("priority", 0),
        }
        try:
            db.insert_message(msg_dict)
        except Exception as e:
            print(f"[master] mcp_tool_call insert_message failed: {e}", flush=True)
        return True, {"msg_id": msg_id, "audit_only": True, "kind": kind,
                       "tool": payload.get("tool")}

    # dispatch_brief 永远 audit_only — 不查 registry, 不 forward
    # to_agent 是 placeholder (e.g. audit.dispatch), 不需 real agent
    if kind == "dispatch_brief":
        if not payload.get("dispatch_id"):
            return False, {"reason": "missing_payload_dispatch_id", "kind": kind}
        msg_id = uuid.uuid4().hex
        msg_dict = {
            "id": msg_id, "ts": time.time(),
            "from_agent": body.get("from_agent", "master.api"),
            "to_agent": agent_id,  # 通常 "audit.dispatch" placeholder
            "from_role": body.get("from_role", "platform"),
            "to_role": "audit",
            "kind": "dispatch_brief",
            "payload": payload,
            "parent_id": body.get("parent_id"),
            "priority": body.get("priority", 0),
        }
        try:
            db.insert_message(msg_dict)
        except Exception as e:
            print(f"[master] dispatch_brief insert_message failed: {e}", flush=True)
        return True, {"msg_id": msg_id, "audit_only": True, "dispatch_id": payload.get("dispatch_id")}

    a = registry.get_agent(agent_id)
    if not a:
        # cold-start race — 触发 rediscover + 重试
        # 加严: 5 次退避 2/5/10/15/30s (总 62s), 防 master + node + 远端 multi-restart 撞 race
        # 中途任一次命中即 return, 不必跑完
        import asyncio as _asyncio
        retry_delays = (2.0, 5.0, 10.0, 15.0, 30.0)
        for attempt, delay in enumerate(retry_delays, start=1):
            # rediscover 给所有 node
            for nid, n in list(registry.nodes.items()):
                if n.ws_writer:
                    rpc = {"jsonrpc": "2.0", "method": "discover_agents", "params": {}}
                    try:
                        await send_to_writer(n.ws_writer, json.dumps(rpc, ensure_ascii=False))
                    except Exception:
                        pass
            await _asyncio.sleep(delay)
            a = registry.get_agent(agent_id)
            if a:
                print(f"[master] forward_send rediscover hit on attempt {attempt} "
                      f"(after {delay}s) for {agent_id}", flush=True)
                break
        if not a:
            return False, {"reason": "agent_not_found", "agent_id": agent_id,
                           "tried_rediscover": True, "attempts": len(retry_delays),
                           "total_wait_sec": sum(retry_delays)}

    node = registry.get_node(a.node_id)
    if not node or not node.ws_writer:
        return False, {"reason": "node_offline", "node_id": a.node_id}

    msg_id = uuid.uuid4().hex
    msg_dict = {
        "id": msg_id,
        "ts": time.time(),
        "from_agent": body.get("from_agent", "master.api"),
        "to_agent": agent_id,
        "from_role": body.get("from_role", "CEO"),
        "to_role": a.role,
        "kind": body.get("kind", "command"),
        "payload": body.get("payload", {}),
        "parent_id": body.get("parent_id"),
        "priority": body.get("priority", 0),
    }
    # 持久化 (作为 outbound 记录, 路由失败也保留以便排查)
    try:
        db.insert_message(msg_dict)
    except Exception as e:
        print(f"[master] insert_message failed: {e}", flush=True)

    # kind=user_direct + from_agent=user.tmux 仅留档, 不 forward 给 node
    # 否则 stop_hook.log_user_prompt.py 留档 user attached tmux 输入时,
    # text 会被 send_to_tmux 重新注入 cli, 触发 agent 二次处理 user message (dup bug).
    from_agent = msg_dict.get("from_agent", "")
    if msg_dict["kind"] == "user_direct" and from_agent.startswith("user."):
        return True, {"msg_id": msg_id, "audit_only": True}

    # ( dispatch_brief 已在 registry.get_agent 之前 short-circuit)

    # 推给 node: command_agent op=send
    rpc = {
        "jsonrpc": "2.0",
        "method": "command_agent",
        "params": {
            "agent_id": agent_id,
            "driver_type": a.driver_type,
            "op": "send",
            "args": {"kind": msg_dict["kind"], "payload": msg_dict["payload"],
                     "msg_id": msg_id},
        },
    }
    try:
        await send_to_writer(node.ws_writer, json.dumps(rpc, ensure_ascii=False))
    except Exception as e:
        return False, {"reason": "ws_send_failed", "detail": str(e), "msg_id": msg_id}
    return True, {"msg_id": msg_id, "node_id": a.node_id, "driver_type": a.driver_type}


async def handle_http(reader, writer, method, path, headers, body, registry, db):
    """处理 HTTP REST 请求"""
    response_body = ""
    status = 200

    # 切掉 query string 用于路由匹配, 解析后供后续 endpoint 用
    if "?" in path:
        route_path, _, query_str = path.partition("?")
        query = {}
        for kv in query_str.split("&"):
            if "=" in kv:
                k, _, v = kv.partition("=")
                query[k] = v
    else:
        route_path = path
        query = {}
    path = route_path  # 后续匹配只看 path 不含 query

    if method == "GET":
        if path == "/" or path == "/healthz":
            response_body = "pre master ok\n"
        elif path == "/api/v1/nodes":
            response_body = json.dumps({"nodes": registry.list_nodes()}, ensure_ascii=False)
        elif path == "/api/v1/agents":
            agents = registry.list_agents()
            # 合并 activity (来自 driver detect_activity, node 心跳上报)
            # + task_title (从 master.db 最近 to_agent 的 command/task_request)
            for a in agents:
                aid = a["agent_id"]
                act = registry.get_activity(aid) or {}
                # 找最近 to_agent=aid 且 kind in (command/task_request) 的 message 抽 title
                recent = db.query_messages(agent_id=aid, limit=20)
                title = None
                title_msg_id = None
                title_from = None
                for m in recent:
                    if m.get("to_agent") != aid:
                        continue
                    if m.get("kind") not in ("command", "task_request", "evaluate_request"):
                        continue
                    payload = m.get("payload", {}) or {}
                    raw = (payload.get("task_title") or payload.get("task")
                           or payload.get("text") or payload.get("prompt") or "")
                    if not raw:
                        continue
                    title = raw[:60].replace("\n", " ")
                    if len(raw) > 60:
                        title += "..."
                    title_msg_id = m.get("id")
                    title_from = m.get("from_agent")
                    break
                # 综合 state: pending 优先 → activity.state → agent.state
                pending = registry.get_pending(aid)
                if pending:
                    # enrich pending 加 prehook_decision (关联当前 ask UI 的 hook entry)
                    cwd = (a.get("metadata") or {}).get("cwd", "")
                    if cwd and "prehook_decision" not in pending:
                        pd = find_active_prehook_for_pending(
                            cwd, pending.get("since_pane_ts"),
                        )
                        # 写到 pending 副本, 不污染 registry 原对象
                        pending = {**pending, "prehook_decision": pd}
                    state = "blocked_user"
                elif act.get("state"):
                    state = act.get("state")
                else:
                    state = a.get("state", "idle")
                # cross-ref 到 task 视角 — 找最近相关 dispatch
                cur_did, cur_drole = db.find_recent_dispatch_for_agent(aid)
                cur_dstatus = None
                if cur_did:
                    # 复用 list_dispatches 推断 status (轻量, 只查这一个 dispatch)
                    one = db.list_dispatches(since=0, limit=1, status_filter=None)
                    # find 这个 dispatch_id (list_dispatches 倒序 limit=1 拿不到; 改用 query_dispatch_events 推断)
                    evts = db.query_dispatch_events(cur_did)
                    if evts:
                        kinds = {e["kind"] for e in evts}
                        if "report" in kinds:
                            cur_dstatus = "done"
                        elif "command" in kinds:
                            cur_dstatus = "executing"
                        elif "task_verdict" in kinds:
                            last_v = next((e for e in reversed(evts) if e["kind"] == "task_verdict"), None)
                            cur_dstatus = "rejected" if (last_v and last_v["payload"].get("approve") is False) else "approved_pending_executor"
                        elif "task_request" in kinds:
                            cur_dstatus = "in_progress_evaluation"
                    # done/rejected/abandoned 视为非 current
                    if cur_dstatus in ("done", "rejected"):
                        cur_did = None
                        cur_drole = None
                # 透传 proposals (agent stop 后 supervised analyzer 生成)
                proposals_entry = registry.get_proposals(aid)
                a["activity"] = {
                    "state": state,
                    "pending": pending,
                    "proposals": proposals_entry,  # null 或 {proposals: [...], ts}
                    "proposals_muted": registry.is_proposals_muted(aid),  # 
                    "last_action": act.get("last_action"),
                    "tool_kind": act.get("tool_kind"),
                    "pane_summary": act.get("pane_summary"),
                    # 新增字段透传
                    "recent_actions": act.get("recent_actions") or [],
                    "last_response_excerpt": act.get("last_response_excerpt"),
                    "claude_status": act.get("claude_status"),
                    # LLM 生成的 20 字任务总结 (60s 后台更新)
                    "task_summary": (registry.get_task_summary(aid) or {}).get("summary"),
                    "task_summary_ts": (registry.get_task_summary(aid) or {}).get("ts"),
                    # task title 来自 master.db (派单历史)
                    "task_title": title,
                    "task_msg_id": title_msg_id,
                    "task_from_agent": title_from,
                    "since_ts": act.get("since_ts"),
                    "last_activity_ts": act.get("last_activity_ts"),
                    # cross-ref 到 task 视角
                    "current_dispatch_id": cur_did,
                    "current_dispatch_role": cur_drole,
                    "current_dispatch_status": cur_dstatus if cur_did else None,
                }
            response_body = json.dumps({"agents": agents}, ensure_ascii=False)
        elif path.startswith("/api/v1/agents/") and path.endswith("/messages"):
            # GET /api/v1/agents/{id}/messages?since=..&limit=..&kind=..
            agent_id = path[len("/api/v1/agents/"):-len("/messages")]
            since = float(query.get("since", "0") or "0")
            limit = int(query.get("limit", "100") or "100")
            kind = query.get("kind", "") or None
            msgs = db.query_messages(agent_id=agent_id, since=since, limit=limit, kind=kind)
            response_body = json.dumps({"agent": agent_id, "messages": msgs}, ensure_ascii=False)
        elif path.startswith("/api/v1/agents/") and path.endswith("/mini-tasks"):
            # GET /api/v1/agents/{id}/mini-tasks?since=&limit=&parent_dispatch_id=
            agent_id = path[len("/api/v1/agents/"):-len("/mini-tasks")]
            since = float(query.get("since", "0") or "0")
            limit = int(query.get("limit", "30") or "30")
            pdid = query.get("parent_dispatch_id", "") or None
            tasks = db.query_mini_tasks(agent_id=agent_id, since=since, limit=limit,
                                         parent_dispatch_id=pdid, include_actions=False)
            response_body = json.dumps({"agent": agent_id, "mini_tasks": tasks},
                                        ensure_ascii=False)
        elif path.startswith("/api/v1/mini-tasks/"):
            # GET /api/v1/mini-tasks/{id} — 单条详情含 actions
            mini_id = path[len("/api/v1/mini-tasks/"):]
            # 校验 id 安全 (mini_task_id 含 agent_id 含点和短横, 长可达 100 字)
            import re as _re
            if not _re.match(r"^[A-Za-z0-9._\-]{1,200}$", mini_id):
                status = 400
                response_body = json.dumps({"error": "invalid mini_task_id"})
            else:
                d = db.get_mini_task(mini_id)
                if d:
                    response_body = json.dumps(d, ensure_ascii=False)
                else:
                    status = 404
                    response_body = json.dumps({"error": "mini_task not found",
                                                 "mini_task_id": mini_id})
        elif path == "/api/v1/mini-tasks":
            # GET /api/v1/mini-tasks?since=&limit=&parent_dispatch_id= — 全 agent 列表
            since = float(query.get("since", "0") or "0")
            limit = int(query.get("limit", "50") or "50")
            pdid = query.get("parent_dispatch_id", "") or None
            tasks = db.query_mini_tasks(agent_id=None, since=since, limit=limit,
                                         parent_dispatch_id=pdid, include_actions=False)
            response_body = json.dumps({"mini_tasks": tasks}, ensure_ascii=False)
        elif path == "/api/v1/pending":
            response_body = json.dumps({"pending": registry.list_pending()}, ensure_ascii=False)
        elif path.startswith("/api/v1/agents/") and path.endswith("/pending"):
            agent_id = path[len("/api/v1/agents/"):-len("/pending")]
            p = registry.get_pending(agent_id)
            response_body = json.dumps({"agent": agent_id, "pending": p}, ensure_ascii=False)
        elif path.startswith("/api/v1/agents/") and path.endswith("/cycle_state"):
            # mcp tool cycle_state endpoint
            # 返 freerun cycle 状态 (从 cycle_alert state.json 读 latest alert)
            agent_id = path[len("/api/v1/agents/"):-len("/cycle_state")]
            try:
                from pathlib import Path as _Path
                _state_path = _Path(_PRE_LOG_ROOT) / "cycle_alert" / "state.json"
                if _state_path.exists():
                    with open(_state_path) as _sf:
                        _state = json.load(_sf)
                else:
                    _state = {"alerts": {}}
                _alerts = _state.get("alerts", {}) or {}
                # 找该 agent 的最后 alert
                _agent_alerts = [
                    a for aid, a in _alerts.items()
                    if isinstance(a, dict) and a.get("agent_id") == agent_id
                ]
                _agent_alerts.sort(key=lambda x: x.get("ts", 0), reverse=True)
                _latest = _agent_alerts[0] if _agent_alerts else None
                response_body = json.dumps({
                    "agent_id": agent_id,
                    "has_freerun_data": bool(_latest),
                    "last_alert": _latest,
                    "total_alerts_for_agent": len(_agent_alerts),
                    "_doc": " cycle_state stub from cycle_alert state.json",
                }, ensure_ascii=False)
            except (OSError, ValueError, json.JSONDecodeError) as _e:
                response_body = json.dumps({
                    "agent_id": agent_id,
                    "has_freerun_data": False,
                    "error": str(_e)[:200],
                })
        elif path.startswith("/api/v1/agents/") and path.endswith("/pane"):
            # — read_pane endpoint
            # G1 REST + G2 server-side 净化 + G3 capability + G6 status enum + G7 fail-closed + G9 audit
            import hashlib as _hashlib
            import re as _re_pane
            agent_id = path[len("/api/v1/agents/"):-len("/pane")]
            # Bearer caller token sha (跟 notify_audit 同模式)
            auth_h = headers.get("authorization", "") or headers.get("Authorization", "")
            caller_token_sha = _hashlib.sha256(auth_h.encode("utf-8")).hexdigest()[:12]
            # G9 限频 30/min
            ok_rate, rate_reason = _read_pane_rate_check(caller_token_sha)
            if not ok_rate:
                _audit_read_pane(caller_token_sha, agent_id, "", 0, {},
                                 "agent_unavailable", False,
                                 "rejected_rate_limit", rate_reason)
                status = 429
                response_body = json.dumps({
                    "status": "agent_unavailable",
                    "error": "rate_limited", "retry_after": 60, "detail": rate_reason
                })
            else:
                # agent_id format check (跟 G1 同源)
                if not _re_pane.match(r"^[a-zA-Z0-9._\-]{1,128}$", agent_id):
                    _audit_read_pane(caller_token_sha, agent_id, "", 0, {},
                                     "agent_unavailable", False,
                                     "rejected_bad_agent_id", "format")
                    status = 400
                    response_body = json.dumps({
                        "status": "agent_unavailable",
                        "error": "invalid agent_id format"
                    })
                else:
                    # G3 capability check
                    cap_ok, cap_reason = _check_read_pane_capability(
                        caller_token_sha, agent_id)
                    if not cap_ok:
                        _audit_read_pane(caller_token_sha, agent_id, "", 0, {},
                                         "agent_unavailable", False,
                                         "rejected_capability", cap_reason)
                        status = 403
                        response_body = json.dumps({
                            "status": "agent_unavailable",
                            "error": "capability_denied", "detail": cap_reason
                        })
                    else:
                        # G4 target_node 推断 (caller 不能传, 防伪造)
                        target_node = agent_id.split(".")[0] if "." in agent_id else "local"
                        # query 参数
                        try:
                            req_lines = max(1, min(1000,
                                int(query.get("lines", "200") or "200")))
                        except (ValueError, TypeError):
                            req_lines = 200
                        grep_pat = (query.get("grep") or "")[:128]
                        raw_mode = (query.get("raw") == "true")
                        i_understand = (query.get("i_understand_risk") == "true")
                        if raw_mode and not i_understand:
                            _audit_read_pane(caller_token_sha, agent_id, target_node,
                                             0, {}, "agent_unavailable", False,
                                             "rejected_raw_without_consent", "")
                            status = 400
                            response_body = json.dumps({
                                "status": "agent_unavailable",
                                "error": "raw=true requires i_understand_risk=true"
                            })
                        else:
                            # 解析 target session: agent_id → tmux session
                            # local agent_id 格式: local.cli-claude-code-local.{project}
                            # tmux session 通常 = project name
                            agent = registry.get_agent(agent_id) if hasattr(
                                registry, "get_agent") else None
                            if agent is None:
                                # 找 agents list 兼容
                                _agents = list(registry.list_agents()) if hasattr(
                                    registry, "list_agents") else []
                                for _a in _agents:
                                    if (_a.get("agent_id") if isinstance(_a, dict)
                                            else getattr(_a, "agent_id", None)) == agent_id:
                                        agent = _a
                                        break
                            session_name = ""
                            if agent is not None:
                                meta = (agent.metadata if hasattr(agent, "metadata")
                                        else (agent.get("metadata") if isinstance(agent, dict)
                                              else {})) or {}
                                session_name = meta.get("tmux_session") or ""
                                if not session_name:
                                    # fallback: project 名 (agent_id 末段)
                                    session_name = agent_id.rsplit(".", 1)[-1] \
                                        if "." in agent_id else agent_id
                            else:
                                session_name = agent_id.rsplit(".", 1)[-1] \
                                    if "." in agent_id else agent_id
                            # 调本地 capture_pane (target_node==local) 或 ws RPC (远端)
                            raw_pane = ""
                            cap_status = "agent_unavailable"
                            cap_error = ""
                            if target_node == "local":
                                try:
                                    _here_src = os.path.dirname(
                                        os.path.dirname(os.path.abspath(__file__)))
                                    if _here_src not in sys.path:
                                        sys.path.insert(0, _here_src)
                                    from tmux_helper import (
                                        capture_pane as _capture_pane,
                                        has_session as _has_session,
                                    )
                                    if _has_session(session_name, timeout=2.0):
                                        raw_pane = _capture_pane(
                                            session_name, lines=req_lines, timeout=5.0)
                                        cap_status = "ok"
                                    else:
                                        cap_status = "agent_unavailable"
                                        cap_error = "tmux_session_not_found"
                                except (ImportError, Exception) as e:  # noqa: BLE001
                                    cap_status = "agent_unavailable"
                                    cap_error = f"{type(e).__name__}: {str(e)[:120]}"
                            else:
                                # 远端 ws RPC (Phase 1 hack: 仅 remote-node 已知 + ws connection 路径)
                                # [remote-node-only hack 自 待 ≥3 node 升级通用 capture
                                # registry, 见 Phase 1 G5]
                                cap_status = "agent_unavailable"
                                cap_error = (f"remote_capture_via_ws_rpc_pending "
                                             f"target_node={target_node} "
                                             f"(Phase 1 pre 后端 routing 仅本机就绪, "
                                             f"远端 ws RPC capture_pane handler 已注册但需"
                                             f" node 端 driver 联动, 跟 dispatch 010 collector "
                                             f"实施同时间窗)")
                            # G2 server-side 三层净化
                            if raw_pane:
                                # 先 ANSI strip (除非 raw=true 显式 i_understand)
                                if raw_mode and i_understand:
                                    sanitized = raw_pane
                                else:
                                    sanitized = _strip_ansi(raw_pane)
                                # 二次净化 _FORBIDDEN_CTRL (防 caller 终端 RCE-like 操控)
                                if not raw_mode:
                                    sanitized = "".join(
                                        c if c not in _FORBIDDEN_CTRL else " "
                                        for c in sanitized)
                                # SENSITIVE_PATTERNS 7 类脱敏
                                redact_hits: dict = {}
                                if not raw_mode:
                                    try:
                                        _here_src = os.path.dirname(
                                            os.path.dirname(os.path.abspath(__file__)))
                                        if _here_src not in sys.path:
                                            sys.path.insert(0, _here_src)
                                        from master.redact import redact as _redact
                                        sanitized, redact_hits = _redact(sanitized)
                                    except ImportError:
                                        pass
                                # grep 过滤 (server-side 后置, 净化后过滤)
                                if grep_pat:
                                    try:
                                        gre = _re_pane.compile(grep_pat)
                                        sanitized = "\n".join(
                                            ln for ln in sanitized.split("\n")
                                            if gre.search(ln))
                                    except _re_pane.error:
                                        pass
                                # truncate 标记 (line_count_returned)
                                lines_arr = sanitized.split("\n")
                                truncated = (len(lines_arr) > req_lines)
                                if truncated:
                                    lines_arr = lines_arr[-req_lines:]
                                    sanitized = "\n".join(lines_arr)
                                line_count_returned = len(lines_arr)
                                if line_count_returned == 0 or \
                                        (line_count_returned == 1 and not lines_arr[0].strip()):
                                    cap_status = "empty"
                                # G2 status idle 判定: pane 非空但仅 cli 提示符 (启发式 — 简化版)
                                # 简化处理: 不强行判 idle, 让 caller 看 content 自决
                                # 真正 idle 判断走 registry.activity (现有路径)
                                resp = {
                                    "status": cap_status,
                                    "agent_id": agent_id,
                                    "target_node": target_node,
                                    "lines": req_lines,
                                    "captured_at_ts": time.time(),
                                    "content": sanitized,
                                    "redacted_patterns_hit": redact_hits,
                                    "truncated": truncated,
                                    "line_count_returned": line_count_returned,
                                    "raw_disclosed": raw_mode,
                                }
                                response_body = json.dumps(resp, ensure_ascii=False)
                                _audit_read_pane(caller_token_sha, agent_id, target_node,
                                                 line_count_returned, redact_hits,
                                                 cap_status, raw_mode,
                                                 "raw_disclosed" if raw_mode else "accepted",
                                                 "")
                            else:
                                # G6 status enum: agent_unavailable 不返 content (防 caller 把 undefined 当 empty)
                                resp = {
                                    "status": cap_status,
                                    "agent_id": agent_id,
                                    "target_node": target_node,
                                    "error": cap_error,
                                }
                                response_body = json.dumps(resp, ensure_ascii=False)
                                _audit_read_pane(caller_token_sha, agent_id, target_node,
                                                 0, {}, cap_status, False,
                                                 "agent_unavailable", cap_error)
                                if cap_status == "agent_unavailable":
                                    status = 502 if "remote_capture" in cap_error else 404
        elif path.startswith("/api/v1/agents/") and path.endswith("/prehook-decisions"):
            # GUI 决策卡显示该 agent 最近 prehook 评价 (allow/deny/ask + reason)
            agent_id = path[len("/api/v1/agents/"):-len("/prehook-decisions")]
            try:
                limit = max(1, min(50, int(query.get("limit", "5") or "5")))
            except ValueError:
                limit = 5
            a = registry.get_agent(agent_id)
            if not a:
                status = 404
                response_body = json.dumps({"error": "agent not found", "agent_id": agent_id})
            else:
                cwd = (a.metadata or {}).get("cwd", "")
                decisions = load_recent_prehook_decisions(cwd, limit=limit)
                response_body = json.dumps({
                    "agent_id": agent_id,
                    "cwd": cwd,
                    "decisions": decisions,
                }, ensure_ascii=False)
        elif (path.startswith("/api/v1/agents/")
                and path.endswith("/transcript/stream")):
            # SSE: GET /api/v1/agents/<id>/transcript/stream?ticket=<x>&tail=<n>
            # auth 已在 _check_auth 里走 ticket 校验; 这里从 ticket 取 caller_token_sha.
            # per-connection 限频 (SSE_MAX_CONN_PER_TOKEN), 不再撞 _read_pane_rate_check 那个共享桶.
            import re as _re_ss
            from master import sse_ticket as _sse_t_ss
            agent_id = path[len("/api/v1/agents/"):-len("/transcript/stream")]
            tk_ctx = _sse_t_ss.peek(query.get("ticket", ""), agent_id)
            if not tk_ctx:
                # _check_auth 早就挡掉了, 这里 defense-in-depth
                status = 401
                response_body = json.dumps({"error": "invalid_or_expired_ticket"})
            elif not _re_ss.match(r"^[a-zA-Z0-9._\-]{1,128}$", agent_id):
                status = 400
                response_body = json.dumps({"error": "invalid_agent_id"})
            else:
                caller_token_sha = tk_ctx["caller_token_sha"]
                active = _SSE_CONN_COUNT.get(caller_token_sha, 0)
                if active >= _SSE_MAX_CONN_PER_TOKEN:
                    status = 429
                    response_body = json.dumps({
                        "error": "too_many_streams",
                        "active": active,
                        "limit": _SSE_MAX_CONN_PER_TOKEN,
                    })
                else:
                    agent = registry.get_agent(agent_id) if hasattr(registry, "get_agent") else None
                    cwd = (agent.metadata if agent else {}).get("cwd", "") if agent else ""
                    transcript_path = _resolve_transcript_path(cwd) if cwd else ""
                    if not transcript_path or not os.path.isfile(transcript_path):
                        status = 404
                        response_body = json.dumps({
                            "error": "transcript_not_found",
                            "agent_id": agent_id,
                            "cwd": cwd,
                        })
                    else:
                        # tail backfill 行数
                        try:
                            tail_n = max(1, min(_TRANSCRIPT_MAX_LIMIT,
                                int(query.get("tail", "100") or "100")))
                        except (ValueError, TypeError):
                            tail_n = 100
                        _SSE_CONN_COUNT[caller_token_sha] = active + 1
                        _audit_agent_data_read(caller_token_sha, "transcript_stream",
                                                agent_id, "", 0, "opened",
                                                "accepted", "")
                        try:
                            await _serve_transcript_sse(
                                writer, transcript_path,
                                caller_token_sha, agent_id, tail_n,
                            )
                        finally:
                            c = _SSE_CONN_COUNT.get(caller_token_sha, 1) - 1
                            if c <= 0:
                                _SSE_CONN_COUNT.pop(caller_token_sha, None)
                            else:
                                _SSE_CONN_COUNT[caller_token_sha] = c
                            _audit_agent_data_read(caller_token_sha, "transcript_stream",
                                                    agent_id, "", 0, "closed",
                                                    "accepted", "")
                        return  # SSE handler 已 writer.close(), 不走默认 JSON 输出
        elif path.startswith("/api/v1/agents/") and path.endswith("/transcript"):
            # 增量读 Claude Code transcript JSONL → UI chat timeline
            # query: ?since=<byte_offset>&limit=<n>
            # 复用 read_pane 安全栈 (capability + rate_limit + audit jsonl)
            import hashlib as _hashlib_t
            import re as _re_t
            agent_id = path[len("/api/v1/agents/"):-len("/transcript")]
            auth_h = headers.get("authorization", "") or headers.get("Authorization", "")
            caller_token_sha = _hashlib_t.sha256(auth_h.encode("utf-8")).hexdigest()[:12]
            ok_rate, rate_reason = _read_pane_rate_check(caller_token_sha)
            if not ok_rate:
                _audit_agent_data_read(caller_token_sha, "transcript", agent_id, "",
                                        0, "rate_limited", "rejected", rate_reason)
                status = 429
                response_body = json.dumps({
                    "status": "rate_limited",
                    "error": "rate_limited", "retry_after": 60,
                })
            elif not _re_t.match(r"^[a-zA-Z0-9._\-]{1,128}$", agent_id):
                _audit_agent_data_read(caller_token_sha, "transcript", agent_id, "",
                                        0, "bad_agent_id", "rejected", "format")
                status = 400
                response_body = json.dumps({"error": "invalid agent_id format"})
            else:
                cap_ok, cap_reason = _check_read_pane_capability(
                    caller_token_sha, agent_id)
                if not cap_ok:
                    _audit_agent_data_read(caller_token_sha, "transcript", agent_id, "",
                                            0, "capability_denied", "rejected", cap_reason)
                    status = 403
                    response_body = json.dumps({
                        "error": "capability_denied", "detail": cap_reason
                    })
                else:
                    target_node = agent_id.split(".")[0] if "." in agent_id else "local"
                    if target_node != "local":
                        _audit_agent_data_read(caller_token_sha, "transcript", agent_id,
                                                target_node, 0, "remote_unavailable",
                                                "rejected", "remote_node_pending")
                        status = 502
                        response_body = json.dumps({
                            "error": "remote_node_unavailable",
                            "target_node": target_node,
                            "detail": "transcript endpoint phase 1 仅支持 local agent",
                        })
                    else:
                        # 解析三种 paging query: since (forward) / before (backward) / tail (initial last N)
                        # 互斥, 优先级 since > before > tail; 都没传等价 tail=limit
                        def _opt_int(qkey: str) -> Optional[int]:
                            v = query.get(qkey)
                            if v is None or v == "":
                                return None
                            try:
                                return max(0, int(v))
                            except (ValueError, TypeError):
                                return None
                        since_byte = _opt_int("since")
                        before_byte = _opt_int("before")
                        tail_n = _opt_int("tail")
                        try:
                            req_limit = max(1, min(_TRANSCRIPT_MAX_LIMIT,
                                int(query.get("limit", "200") or "200")))
                        except (ValueError, TypeError):
                            req_limit = 200
                        agent = registry.get_agent(agent_id) if hasattr(
                            registry, "get_agent") else None
                        cwd = ""
                        if agent is not None:
                            meta = (agent.metadata if hasattr(agent, "metadata")
                                    else (agent.get("metadata") if isinstance(agent, dict)
                                          else {})) or {}
                            cwd = meta.get("cwd", "") or ""
                        if not cwd:
                            _audit_agent_data_read(caller_token_sha, "transcript", agent_id,
                                                    target_node, 0, "no_cwd", "rejected",
                                                    "agent_metadata_missing_cwd")
                            status = 404
                            response_body = json.dumps({
                                "error": "agent_cwd_unknown", "agent_id": agent_id,
                            })
                        else:
                            # 默认读当前 session; ?session=<uuid> 切到历史 session
                            req_session = (query.get("session") or "").strip()
                            if req_session:
                                tp = _resolve_session_transcript(cwd, req_session)
                                if not tp:
                                    _audit_agent_data_read(caller_token_sha, "transcript",
                                                            agent_id, target_node, 0,
                                                            "session_not_found", "rejected",
                                                            f"session={req_session[:40]}")
                                    status = 404
                                    response_body = json.dumps({
                                        "error": "session_not_found",
                                        "session_id": req_session,
                                    })
                                    tp = ""
                            else:
                                tp = _resolve_transcript_path(cwd)
                            if not tp and not req_session:
                                _audit_agent_data_read(caller_token_sha, "transcript",
                                                        agent_id, target_node, 0,
                                                        "no_transcript", "rejected",
                                                        "transcript_path_marker_missing")
                                status = 404
                                response_body = json.dumps({
                                    "error": "transcript_unavailable",
                                    "detail": "agent 尚未触发过 PreToolUse, 无 transcript 记录",
                                    "agent_id": agent_id,
                                })
                            elif tp:
                                result = _read_transcript_window(
                                    tp,
                                    since_byte=since_byte,
                                    before_byte=before_byte,
                                    tail_n=tail_n,
                                    limit=req_limit,
                                )
                                # bytes_returned 仅 forward 模式有意义, 其余按 0 记 audit
                                bytes_returned = (result["next_since"] - (since_byte or 0)) \
                                    if (since_byte is not None and since_byte > 0) else 0
                                _audit_agent_data_read(caller_token_sha, "transcript",
                                                        agent_id, target_node,
                                                        max(0, bytes_returned), "ok", "accepted",
                                                        f"msgs={len(result['messages'])}"
                                                        f" mode={'fwd' if since_byte else ('bwd' if before_byte else 'tail')}")
                                response_body = json.dumps({
                                    "agent_id": agent_id,
                                    "messages": result["messages"],
                                    "next_since": result["next_since"],
                                    "prev_before": result["prev_before"],
                                    "eof": result["eof"],
                                    "total_size": result["total_size"],
                                    "transcript_id": result["transcript_id"],
                                    "reset_signal": result["reset_signal"],
                                }, ensure_ascii=False)
        elif path.startswith("/api/v1/agents/") and path.endswith("/sessions"):
            # 列 agent 的 transcript 历史 session (~/.claude/projects/<slug>/*.jsonl)
            # /clear 不删旧文件, 这里把它们都列出来给 UI session dropdown 用
            import hashlib as _hashlib_s
            import re as _re_s
            agent_id = path[len("/api/v1/agents/"):-len("/sessions")]
            auth_h = headers.get("authorization", "") or headers.get("Authorization", "")
            caller_token_sha = _hashlib_s.sha256(auth_h.encode("utf-8")).hexdigest()[:12]
            ok_rate, rate_reason = _read_pane_rate_check(caller_token_sha)
            if not ok_rate:
                _audit_agent_data_read(caller_token_sha, "sessions", agent_id, "",
                                        0, "rate_limited", "rejected", rate_reason)
                status = 429
                response_body = json.dumps({
                    "error": "rate_limited", "retry_after": 60,
                })
            elif not _re_s.match(r"^[a-zA-Z0-9._\-]{1,128}$", agent_id):
                status = 400
                response_body = json.dumps({"error": "invalid agent_id format"})
            else:
                cap_ok, cap_reason = _check_read_pane_capability(
                    caller_token_sha, agent_id)
                if not cap_ok:
                    _audit_agent_data_read(caller_token_sha, "sessions", agent_id, "",
                                            0, "capability_denied", "rejected", cap_reason)
                    status = 403
                    response_body = json.dumps({
                        "error": "capability_denied", "detail": cap_reason
                    })
                else:
                    target_node = agent_id.split(".")[0] if "." in agent_id else "local"
                    if target_node != "local":
                        status = 502
                        response_body = json.dumps({
                            "error": "remote_node_unavailable",
                            "target_node": target_node,
                        })
                    else:
                        agent = registry.get_agent(agent_id) if hasattr(
                            registry, "get_agent") else None
                        cwd = ""
                        if agent is not None:
                            meta = (agent.metadata if hasattr(agent, "metadata")
                                    else (agent.get("metadata") if isinstance(agent, dict)
                                          else {})) or {}
                            cwd = meta.get("cwd", "") or ""
                        if not cwd:
                            status = 404
                            response_body = json.dumps({
                                "error": "agent_cwd_unknown", "agent_id": agent_id,
                            })
                        else:
                            sessions = _list_agent_sessions(cwd)
                            _audit_agent_data_read(caller_token_sha, "sessions",
                                                    agent_id, target_node, 0, "ok",
                                                    "accepted",
                                                    f"sessions={len(sessions)}")
                            response_body = json.dumps({
                                "agent_id": agent_id,
                                "sessions": sessions,
                            }, ensure_ascii=False)
        elif path.startswith("/api/v1/agents/") and path.endswith("/file"):
            # 读 agent cwd 下的相对路径文件 (UI markdown preview)
            # query: ?path=<relpath>  后缀白名单 .md/.txt; resolve 后必须在 cwd 内
            import hashlib as _hashlib_f
            import re as _re_f
            agent_id = path[len("/api/v1/agents/"):-len("/file")]
            auth_h = headers.get("authorization", "") or headers.get("Authorization", "")
            caller_token_sha = _hashlib_f.sha256(auth_h.encode("utf-8")).hexdigest()[:12]
            ok_rate, rate_reason = _read_pane_rate_check(caller_token_sha)
            if not ok_rate:
                _audit_agent_data_read(caller_token_sha, "file", agent_id, "",
                                        0, "rate_limited", "rejected", rate_reason)
                status = 429
                response_body = json.dumps({"error": "rate_limited", "retry_after": 60})
            elif not _re_f.match(r"^[a-zA-Z0-9._\-]{1,128}$", agent_id):
                _audit_agent_data_read(caller_token_sha, "file", agent_id, "",
                                        0, "bad_agent_id", "rejected", "format")
                status = 400
                response_body = json.dumps({"error": "invalid agent_id format"})
            else:
                cap_ok, cap_reason = _check_read_pane_capability(
                    caller_token_sha, agent_id)
                if not cap_ok:
                    _audit_agent_data_read(caller_token_sha, "file", agent_id, "",
                                            0, "capability_denied", "rejected", cap_reason)
                    status = 403
                    response_body = json.dumps({
                        "error": "capability_denied", "detail": cap_reason
                    })
                else:
                    target_node = agent_id.split(".")[0] if "." in agent_id else "local"
                    if target_node != "local":
                        _audit_agent_data_read(caller_token_sha, "file", agent_id,
                                                target_node, 0, "remote_unavailable",
                                                "rejected", "remote_node_pending")
                        status = 502
                        response_body = json.dumps({
                            "error": "remote_node_unavailable",
                            "target_node": target_node,
                        })
                    else:
                        rel_path = (query.get("path") or "")[:512]
                        agent = registry.get_agent(agent_id) if hasattr(
                            registry, "get_agent") else None
                        cwd = ""
                        if agent is not None:
                            meta = (agent.metadata if hasattr(agent, "metadata")
                                    else (agent.get("metadata") if isinstance(agent, dict)
                                          else {})) or {}
                            cwd = meta.get("cwd", "") or ""
                        # cwd 是 agent.metadata 里, 可能是 ~/foo 形式 — expand
                        if cwd:
                            cwd = os.path.expanduser(cwd)
                        if not cwd or not os.path.isdir(cwd):
                            _audit_agent_data_read(caller_token_sha, "file", agent_id,
                                                    target_node, 0, "no_cwd", "rejected",
                                                    "agent_cwd_missing")
                            status = 404
                            response_body = json.dumps({
                                "error": "agent_cwd_unknown", "agent_id": agent_id,
                            })
                        else:
                            abs_path, err = _resolve_agent_file(cwd, rel_path)
                            if err:
                                _audit_agent_data_read(caller_token_sha, "file", agent_id,
                                                        target_node, 0, "path_rejected",
                                                        "rejected", err)
                                status = 400 if err in (
                                    "missing_cwd_or_path", "absolute_path_rejected",
                                    "null_byte_rejected") else (
                                    403 if err == "path_escapes_cwd" else 404)
                                response_body = json.dumps({"error": err})
                            else:
                                try:
                                    sz = os.path.getsize(abs_path)
                                    if sz > _AGENT_FILE_SIZE_CAP:
                                        _audit_agent_data_read(caller_token_sha, "file",
                                                                agent_id, target_node,
                                                                sz, "too_large", "rejected",
                                                                f"size={sz}")
                                        status = 413
                                        response_body = json.dumps({
                                            "error": "file_too_large",
                                            "size": sz, "cap": _AGENT_FILE_SIZE_CAP,
                                        })
                                    else:
                                        with open(abs_path, "r", encoding="utf-8",
                                                  errors="replace") as f:
                                            content = f.read()
                                        _audit_agent_data_read(caller_token_sha, "file",
                                                                agent_id, target_node,
                                                                len(content.encode("utf-8")),
                                                                "ok", "accepted",
                                                                f"path={rel_path[:80]}")
                                        response_body = json.dumps({
                                            "agent_id": agent_id,
                                            "path": rel_path,
                                            "size": sz,
                                            "content": content,
                                        }, ensure_ascii=False)
                                except OSError as e:
                                    _audit_agent_data_read(caller_token_sha, "file",
                                                            agent_id, target_node, 0,
                                                            "io_error", "rejected",
                                                            str(e)[:120])
                                    status = 500
                                    response_body = json.dumps({"error": "io_error"})
        elif path == "/api/v1/dispatches":
            # 列历次派单概览
            since = float(query.get("since", "0") or "0")
            limit = int(query.get("limit", "50") or "50")
            status_filter = query.get("status") or None  # 不能用 'status' (会覆盖 HTTP status code)
            disps = db.list_dispatches(since=since, limit=limit, status_filter=status_filter)
            response_body = json.dumps({"dispatches": disps}, ensure_ascii=False)
        elif path.startswith("/api/v1/agents/") and path.endswith("/proposals"):
            # GET 单 agent 的 proposals + muted 字段
            aid = path[len("/api/v1/agents/"):-len("/proposals")]
            p = registry.get_proposals(aid)
            muted = registry.is_proposals_muted(aid)
            response_body = json.dumps({"agent": aid, "proposals": p, "muted": muted}, ensure_ascii=False)
        elif path == "/api/v1/dispatches/index":
            # agent-ceo 7 状态对齐版本
            since = float(query.get("since", "0") or "0")
            limit = int(query.get("limit", "100") or "100")
            state_filter = query.get("state") or "all"
            disps = db.list_dispatches_indexed(state_filter=state_filter, since=since, limit=limit)
            response_body = json.dumps({"dispatches": disps}, ensure_ascii=False)
        elif path.startswith("/api/v1/dispatches/") and "/" not in path[len("/api/v1/dispatches/"):]:
            # 单 dispatch 完整流转
            # 限 path 末段 (不含 /), 不抢吃 /timeline /markdown /export 子路径
            did = path[len("/api/v1/dispatches/"):]
            events_raw = db.query_dispatch_events(did)
            if not events_raw:
                status = 404
                response_body = json.dumps({"error": "dispatch_not_found", "dispatch_id": did})
            else:
                # 简化 payload 为 summary (前 200 字)
                events = []
                actors = set()
                for e in events_raw:
                    payload_summary = (e["payload"].get("text") or e["payload"].get("task")
                                       or e["payload"].get("comment")
                                       or e["payload"].get("summary")
                                       or json.dumps(e["payload"], ensure_ascii=False))[:200]
                    events.append({
                        "ts": e["ts"], "kind": e["kind"],
                        "from_agent": e["from_agent"], "from_role": e["from_role"],
                        "to_agent": e["to_agent"], "to_role": e["to_role"],
                        "msg_id": e["id"],
                        "payload_summary": payload_summary,
                    })
                    if e["from_agent"]: actors.add(e["from_agent"])
                    if e["to_agent"]: actors.add(e["to_agent"])
                # 推断 status (复用 list_dispatches 的同样逻辑)
                kinds = {e["kind"] for e in events_raw}
                if "report" in kinds:
                    dstatus = "done"
                elif "command" in kinds:
                    dstatus = "executing"
                elif "task_verdict" in kinds:
                    last_verdict = next((e for e in reversed(events_raw) if e["kind"] == "task_verdict"), None)
                    approve = last_verdict["payload"].get("approve") if last_verdict else None
                    dstatus = "rejected" if approve is False else "approved_pending_executor"
                elif "task_request" in kinds:
                    dstatus = "in_progress_evaluation"
                else:
                    dstatus = "unknown"
                # cross-ref 到 agent 视角 — 加每个 actor 当前 state + summary
                actor_states = {}
                active_actors = []
                for actor_id in actors:
                    a_info = registry.get_agent(actor_id)
                    if not a_info:
                        actor_states[actor_id] = {"state": "unknown"}
                        continue
                    act = registry.get_activity(actor_id) or {}
                    a_state = act.get("state") or a_info.state
                    a_sum = (registry.get_task_summary(actor_id) or {}).get("summary")
                    info = {"state": a_state, "task_summary": a_sum}
                    actor_states[actor_id] = info
                    if a_state in ("busy", "blocked_user"):
                        active_actors.append({"agent_id": actor_id, **info})
                response_body = json.dumps({
                    "dispatch_id": did,
                    "summary": {
                        "started_ts": events_raw[0]["ts"],
                        "last_ts": events_raw[-1]["ts"],
                        "actors": sorted(list(actors)),
                        "status": dstatus,
                        "msg_count": len(events_raw),
                        # cross-ref to agent 视角
                        "active_actors": active_actors,
                        "actor_states": actor_states,
                    },
                    "events": events,
                }, ensure_ascii=False)
        elif path == "/api/v1/notify/audit":
            # read-only mobile_audit 暴露给 pre_ui UI
            # + (T-i fail-closed / T-iii 限频 30/min / T-iv VIRTUAL_AGENTS hardcoded)
            # + 子条款 (b) 字段白名单 + SENSITIVE_PATTERNS 6 类前置脱敏
            # + 严守 since ≤30 天 / limit ≤500 / 9 字段固定不许多
            import hashlib as _hashlib
            import re as _re
            # Bearer 鉴权已在上层 _check_auth 走过, 这里不重做; 取 token sha256[:12] 做限频 key
            auth_h = headers.get("authorization", "") or headers.get("Authorization", "")
            bearer_key = _hashlib.sha256(auth_h.encode("utf-8")).hexdigest()[:12]
            ok_rate, rate_reason = _audit_rate_check(bearer_key)
            if not ok_rate:
                status = 429
                response_body = json.dumps({"error": "rate_limited", "retry_after": 60,
                                              "detail": rate_reason})
            else:
                # query 参数 (server-side 严校)
                now_ts = time.time()
                # since: unix ts, 默 30 天前; 强制 ≥now-30天 ()
                try:
                    since = float(query.get("since", "") or 0)
                except (ValueError, TypeError):
                    since = 0
                min_since = now_ts - 30 * 86400
                if since < min_since:
                    since = min_since
                # limit: max 500
                try:
                    limit = int(query.get("limit", "200") or "200")
                except (ValueError, TypeError):
                    limit = 200
                limit = max(1, min(500, limit))
                # filter 白名单
                f_priority = query.get("priority", "") or None
                f_from_agent = query.get("from_agent", "") or None
                f_channel = query.get("channel", "") or None
                # priority 白名单
                if f_priority and f_priority not in PRIORITY_WHITELIST:
                    f_priority = None
                # channel 白名单
                if f_channel and f_channel not in {"webhook-notify", "cli_sendkeys", "master_log"}:
                    f_channel = None
                # 读 mobile_audit_*.jsonl (ts >= since)
                from pathlib import Path as _Path
                audit_dir = _Path(_PRE_LOG_ROOT) / "cron"
                from datetime import datetime as _dt, timezone as _tz
                cutoff_dt = _dt.fromtimestamp(since, tz=_tz.utc) if since > 0 else None
                rows: list[dict] = []
                truncated = False
                if audit_dir.exists():
                    files = sorted(audit_dir.glob("mobile_audit_*.jsonl"), reverse=True)
                    for f in files:
                        # 文件日期粗筛 (e.g. mobile_audit_20260430.jsonl)
                        try:
                            with open(f, encoding="utf-8") as fh:
                                for line in fh:
                                    try:
                                        e = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue
                                    # ts since 过滤
                                    try:
                                        e_ts_dt = _dt.fromisoformat(
                                            (e.get("ts") or "").replace("Z", "+00:00"))
                                        e_ts = e_ts_dt.timestamp()
                                    except (ValueError, AttributeError):
                                        continue
                                    if e_ts < since:
                                        continue
                                    # : to_user 必 ∈ VIRTUAL_AGENTS (仅 user.default)
                                    if e.get("to_user") not in VIRTUAL_AGENTS:
                                        continue
                                    # filter
                                    if f_priority and e.get("priority") != f_priority:
                                        continue
                                    if f_from_agent and f_from_agent not in (e.get("from_agent") or ""):
                                        continue
                                    if f_channel and e.get("channel") != f_channel:
                                        continue
                                    # 9 字段固定输出 ((b) 字段白名单)
                                    rows.append({
                                        "ts": e.get("ts"),
                                        "from_agent": e.get("from_agent"),
                                        "to_user": e.get("to_user"),
                                        "priority": e.get("priority"),
                                        "channel": e.get("channel"),
                                        "status": e.get("status"),
                                        "error": e.get("error", ""),
                                        "payload_size": e.get("payload_size", 0),
                                        # text_preview 已脱敏 (写时一次跑); 老 audit 无此字段返 ""
                                        "text_preview": e.get("text_preview", ""),
                                    })
                                    if len(rows) >= limit:
                                        truncated = True
                                        break
                        except OSError:
                            continue
                        if len(rows) >= limit:
                            break
                # 按 ts 倒序 (最新在前)
                rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
                response_body = json.dumps({
                    "audit": rows,
                    "total": len(rows),
                    "truncated": truncated,
                }, ensure_ascii=False)
        elif path == "/api/v1/governance/p0_debt":
            # Phase E (DR-DISPATCHER-GATE-1 + ):
            # 提供 pre 端 endpoint, dispatcher 自实施关卡 logic.
            # 返当前 P0 unresolved findings + deadline within 14d list.
            try:
                _now = time.time()
                _14d_ahead = _now + 14 * 86400
                cur = db.conn.execute(
                    "SELECT id, finding_id, priority, title, detail, deadline_ts, "
                    "created_ts, status FROM governance_debts "
                    "WHERE status='unresolved' AND priority='P0' "
                    "ORDER BY deadline_ts ASC NULLS LAST"
                )
                rows = cur.fetchall()
                p0_findings = []
                p0_within_14d = 0
                for r in rows:
                    _entry = {
                        "id": r[0], "finding_id": r[1], "priority": r[2],
                        "title": r[3], "detail": r[4],
                        "deadline_ts": r[5], "created_ts": r[6], "status": r[7],
                    }
                    p0_findings.append(_entry)
                    if r[5] and float(r[5]) <= _14d_ahead:
                        p0_within_14d += 1
                response_body = json.dumps({
                    "p0_count": len(p0_findings),
                    "p0_within_14d_count": p0_within_14d,
                    "p0_findings": p0_findings,
                    "sot": "governance_debts",
                    "ts": _now,
                    "_doc": "DR-DISPATCHER-GATE-1: dispatcher 自实施关卡 logic, p0_count>0 AND deadline within 14d → block 分发",
                }, ensure_ascii=False)
            except Exception as _e:
                status = 500
                response_body = json.dumps({"error": "governance_query_failed", "detail": str(_e)[:200]})
        elif path == "/api/v1/usage/last_success":
            # Phase A v2 — DB SOT (HC-DRLI-5).
            # M13 agent-security: caller_node scope ACL + Bearer + 限频 30/min/caller + 出口 SENSITIVE_PATTERNS.
            # Bearer 已在 _check_auth 走过. 这里加限频 + 出口脱敏.
            import hashlib as _hashlib_ls
            auth_h_ls = headers.get("authorization", "") or headers.get("Authorization", "")
            token_ls = ""
            if auth_h_ls.lower().startswith("bearer "):
                token_ls = auth_h_ls[7:].strip()
            elif "token" in query:
                token_ls = query.get("token", "")
            caller_token_sha_ls = (_hashlib_ls.sha256(token_ls.encode()).hexdigest()[:12]
                                    if token_ls else "anonymous")
            # 限频 30/min/caller (M13)
            ok_rate, rate_reason = _audit_rate_check(caller_token_sha_ls)  # 复用 dispatch 004 audit 限频
            if not ok_rate:
                status = 429
                response_body = json.dumps({"error": "rate_limited",
                                              "reason": rate_reason,
                                              "limit": "30/min/caller"})
            else:
                # caller_node scope ACL: query.node filter (caller 限自己 node, no node = all 但仅 internal)
                node_filter = query.get("node", "")
                cli_filter = query.get("cli_type", "")
                rows = db.query_last_success(
                    node_id=node_filter or None,
                    cli_type=cli_filter or None,
                )
                # 出口 SENSITIVE_PATTERNS 防御 (M13): redact 各 string 字段
                try:
                    from master.redact import redact as _redact_ls
                    for _r in rows:
                        for _k in ("raw_excerpt", "agent_id", "session_id", "model"):
                            _v = _r.get(_k)
                            if isinstance(_v, str) and _v:
                                _sanitized, _hits_ls = _redact_ls(_v)
                                if _sanitized != _v:
                                    _r[_k] = _sanitized
                except ImportError:
                    pass  # fail-safe: redact 不可用不阻
                response_body = json.dumps({
                    "data": rows,
                    "sot": "last_success_per_node",
                    "ts": time.time(),
                    "filter": {
                        "node_id": node_filter or None,
                        "cli_type": cli_filter or None,
                    },
                    "count": len(rows),
                }, ensure_ascii=False)
        elif path == "/api/v1/usage":
            # (user ): account 升主键, db 唯一 SoT, snapshots[] 形态.
            # 新返结构: {"snapshots": [{provider, account, status, used_pct, reset_at,
            # fetch_ts, age_sec, stale, collected_by_node, parsed}, ...],
            # "ts": now, "sot": "usage_snapshot_v2"}
            # legacy 字段 (claude/gemini/codex/by_node) 仍 derive 一份, pre_ui + sys_beep
            # 渐进 adopt 后 ≥30 天下线.
            node_q = query.get("node", "")
            fmt_q = query.get("format", "")  # ?format=v2 → 只返 snapshots[]
            now_ts = time.time()

            def _augment(d: dict) -> dict:
                """加 stale / last_success 字段, 不修改原 dict."""
                out = dict(d)
                pts = d.get("probed_ts")
                if pts:
                    age = now_ts - float(pts)
                    out["age_sec"] = round(age, 1)
                    out["stale"] = age > 1800  # 30min 未更新算 stale
                else:
                    out["stale"] = True
                # 每个 provider 也加 stale (基于自己的 probed_ts 如有)
                for p in ("claude", "claude_agent-research", "gemini", "codex"):
                    pd = out.get(p)
                    if isinstance(pd, dict):
                        pp = pd.get("probed_ts")
                        if pp:
                            pd_age = now_ts - float(pp)
                            pd["age_sec"] = round(pd_age, 1)
                            pd["stale"] = pd_age > 1800
                return out

            usage_data = _augment(registry.usage or {})
            # 总加 by_node 字段供 GUI 切换 (各 node 都 augment)
            by_node_aug = {}
            for nid, nd in (registry.usage_by_node or {}).items():
                by_node_aug[nid] = _augment(nd)
            usage_data["by_node"] = by_node_aug
            usage_data["nodes_known"] = sorted(set(registry.usage_by_node.keys()) | {"local"})

            # (user ): DB 是 SOT, 永远返 usage_snapshot 表最新 + fetch_ts.
            # registry.usage 内存退化为 GUI 备用 (现有字段保留兼容).
            try:
                snap = db.query_usage_snapshot(["claude", "gemini", "codex"])
                fetch_ts_map = {}
                for prov, row in (snap or {}).items():
                    fetch_ts_map[prov] = row.get("fetch_ts")
                    # 同步覆盖到 usage_data 顶层 (DB SOT)
                    db_provider = {
                        "status": row.get("status"),
                        "models": row.get("models"),
                        "used_pct": row.get("used_pct"),
                        "reset_at": row.get("reset_at"),
                        "fetch_ts": row.get("fetch_ts"),
                        "source": row.get("source"),
                    }
                    if row.get("fetch_ts"):
                        db_provider["fetch_age_sec"] = round(
                            now_ts - float(row["fetch_ts"]), 1)
                        db_provider["stale"] = (
                            now_ts - float(row["fetch_ts"])) > 1800
                    usage_data[prov] = db_provider
                usage_data["fetch_ts"] = fetch_ts_map
                usage_data["sot"] = "usage_snapshot_table"
            except Exception:  # noqa: BLE001 — fail-safe, 退化用 registry.usage
                pass

            # snapshots[] 主体 (account-keyed, db SoT)
            # stale=true (age > 1800s) 默认过滤, 防消费方拉到 8h 前的坏数据展示.
            # ?include_stale=true 显式才返 stale entries.
            include_stale = str(query.get("include_stale", "")).lower() in ("1", "true", "yes")
            try:
                rows = db.query_usage_snapshot_v2()
            except Exception:  # noqa: BLE001
                rows = []
            snapshots = []
            for r in rows:
                ft = r.get("fetch_ts")
                age = (now_ts - float(ft)) if ft else None
                snap_entry = {
                    "provider": r.get("provider"),
                    "account": r.get("account"),
                    "status": r.get("status"),
                    "used_pct": r.get("used_pct"),
                    "reset_at": r.get("reset_at"),
                    "fetch_ts": ft,
                    "age_sec": round(age, 1) if age is not None else None,
                    "stale": (age is not None and age > 1800),
                    "collected_by_node": r.get("collected_by_node"),
                    "parsed": r.get("parsed") or {},
                }
                snapshots.append(snap_entry)
            if not include_stale:
                snapshots = [s for s in snapshots if not s.get("stale")]
            usage_data["snapshots"] = snapshots
            usage_data["sot"] = "usage_snapshot_v2"

            if fmt_q == "v2":
                response_body = json.dumps({
                    "snapshots": snapshots,
                    "ts": now_ts,
                    "sot": "usage_snapshot_v2",
                }, ensure_ascii=False)
            elif node_q and node_q != "all" and node_q != "local":
                # 单 node 过滤 — 返该 node 的 {claude, gemini, codex, ..., stale} (legacy)
                node_data = _augment(registry.usage_by_node.get(node_q) or {})
                response_body = json.dumps({
                    **node_data,
                    "_node_id": node_q,
                    "_known_nodes": usage_data["nodes_known"],
                    "snapshots": [s for s in snapshots if s.get("collected_by_node") == node_q],
                }, ensure_ascii=False)
            else:
                response_body = json.dumps(usage_data, ensure_ascii=False)
        elif path.startswith("/api/v1/dispatches/") and path.endswith("/timeline"):
            # 拉单 dispatch 的 message timeline (按 ts 升序) + meta
            did = path[len("/api/v1/dispatches/"):-len("/timeline")]
            events_raw = db.query_dispatch_events(did)
            events = []
            for e in events_raw:
                payload = e.get("payload") or {}
                text = (payload.get("text") or payload.get("brief")
                        or payload.get("summary") or "") if isinstance(payload, dict) else ""
                events.append({
                    "ts": e.get("ts"),
                    "msg_id": e.get("id"),
                    "from_agent": e.get("from_agent"),
                    "to_agent": e.get("to_agent"),
                    "from_role": e.get("from_role"),
                    "kind": e.get("kind"),
                    "text_preview": (text or "")[:300],
                })
            # meta 复用 list_dispatches aggregate
            disps = db.list_dispatches(since=0, limit=200)
            meta = next((d for d in disps if d.get("dispatch_id") == did), {})
            response_body = json.dumps({
                "dispatch_id": did,
                "meta": meta,
                "events": events,
                "event_count": len(events),
            }, ensure_ascii=False)
        elif path.startswith("/api/v1/dispatches/") and path.endswith("/markdown"):
            # 返已生成的 markdown (raw, 给 pre_ui 渲染)
            did = path[len("/api/v1/dispatches/"):-len("/markdown")]
            md_path = os.path.join(_PRE_LOG_ROOT, "tasks", f"{did}.md")
            if not os.path.isfile(md_path):
                status = 404
                response_body = json.dumps({"error": "task_doc_not_found", "dispatch_id": did,
                                            "hint": "POST /api/v1/dispatches/{id}/export 先生成"})
            else:
                try:
                    with open(md_path, encoding="utf-8") as f:
                        md_content = f.read()
                    body_bytes = md_content.encode("utf-8")
                    status_line = "200 OK"
                    response = (
                        f"HTTP/1.1 {status_line}\r\n"
                        "Content-Type: text/markdown; charset=utf-8\r\n"
                        f"Content-Length: {len(body_bytes)}\r\n"
                        "Connection: close\r\n"
                        "\r\n"
                    ).encode("ascii") + body_bytes
                    writer.write(response)
                    await writer.drain()
                    writer.close()
                    return
                except OSError as e:
                    status = 500
                    response_body = json.dumps({"error": str(e)[:200]})
        elif path.startswith("/api/v1/files/") and len(path) > len("/api/v1/files/"):
            # GET /api/v1/files/{file_id} 下载
            import hashlib
            file_id = path[len("/api/v1/files/"):]
            # ACL: requester 必须是 owner / recipient / 任 audit.* 身份
            requester = headers.get("x-agent-id", "") or "?"
            meta = _FILE_META.get(file_id)
            if not meta:
                status = 404
                response_body = json.dumps({"error": "file_not_found"})
            elif requester not in (meta.get("owner"), meta.get("recipient")) \
                    and not requester.startswith("audit."):
                status = 403
                response_body = json.dumps({"error": "forbidden", "file_id": file_id})
            else:
                # 限频
                allowed, rl_reason = _file_rate_check(requester, "download")
                if not allowed:
                    status = 429
                    response_body = json.dumps({"error": "rate_limited", "reason": rl_reason})
                else:
                    fp = meta.get("path", "")
                    if not fp or not os.path.isfile(fp):
                        status = 410  # gone (文件被 rotation 删了)
                        response_body = json.dumps({"error": "file_expired"})
                    else:
                        try:
                            with open(fp, "rb") as f:
                                content = f.read()
                            _audit_file({
                                "ts": time.time(),
                                "op": "download",
                                "agent_id": requester,
                                "file_id": file_id,
                                "size": len(content),
                                "status": "ok",
                            })
                            # 二进制响应 (跟 JSON 路径分开)
                            body_bytes = content
                            content_type = "application/octet-stream"
                            file_name = meta.get("name", "file")
                            status_line = "200 OK"
                            response = (
                                f"HTTP/1.1 {status_line}\r\n"
                                f"Content-Type: {content_type}\r\n"
                                f"Content-Length: {len(body_bytes)}\r\n"
                                f"Content-Disposition: attachment; filename=\"{file_name}\"\r\n"
                                "Connection: close\r\n"
                                "\r\n"
                            ).encode("ascii") + body_bytes
                            writer.write(response)
                            await writer.drain()
                            writer.close()
                            return  # 短路, 不走 JSON 输出
                        except OSError as e:
                            status = 500
                            response_body = json.dumps({"error": str(e)[:200]})
        elif path == "/api/v1/messages":
            since = float(query.get("since", "0") or "0")
            limit = int(query.get("limit", "100") or "100")
            kind = query.get("kind", "") or None
            msgs = db.query_messages(since=since, limit=limit, kind=kind)
            response_body = json.dumps({"messages": msgs}, ensure_ascii=False)
        elif path == "/api/v1/user-decisions":
            # user_decisions extractor (user ): list endpoint
            # query: ?status=pending|resolved|dismissed&limit=50 (default status=pending)
            try:
                _here_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                if _here_src not in sys.path:
                    sys.path.insert(0, _here_src)
                from user_decisions import list_decisions as _ud_list
                f_status = query.get("status", "pending") or None
                if f_status == "all":
                    f_status = None
                try:
                    f_limit = max(1, min(200, int(query.get("limit", "50") or "50")))
                except (ValueError, TypeError):
                    f_limit = 50
                items = _ud_list(status=f_status, limit=f_limit)
                response_body = json.dumps({
                    "decisions": items,
                    "filter_status": f_status or "all",
                    "limit": f_limit,
                }, ensure_ascii=False)
            except ImportError as e:
                status = 503
                response_body = json.dumps({"error": "user_decisions module unavailable",
                                              "detail": str(e)[:200]})
        elif path.startswith("/api/v1/user-decisions/"):
            # GET /api/v1/user-decisions/{id} 详情
            decision_id = path[len("/api/v1/user-decisions/"):]
            if not decision_id or "/" in decision_id:
                status = 400
                response_body = json.dumps({"error": "invalid decision_id"})
            else:
                try:
                    _here_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    if _here_src not in sys.path:
                        sys.path.insert(0, _here_src)
                    from user_decisions import get_decision as _ud_get
                    d = _ud_get(decision_id)
                    if d is None:
                        status = 404
                        response_body = json.dumps({"error": "decision not found",
                                                      "decision_id": decision_id})
                    else:
                        response_body = json.dumps(d, ensure_ascii=False)
                except ImportError as e:
                    status = 503
                    response_body = json.dumps({"error": "user_decisions module unavailable",
                                                  "detail": str(e)[:200]})
        else:
            status = 404
            response_body = json.dumps({"error": "not found", "path": path})
    elif method == "POST":
        if path == "/api/v1/auth/sse-ticket":
            # 用 Bearer (已通过 _check_auth + bus.pane.read scope 验证) 换短 TTL ticket,
            # 给 EventSource 当 ?ticket=<x>. body: {"agent_id": "<id>"}.
            import hashlib as _h_tk
            import re as _re_tk
            from master import sse_ticket as _sse_ticket
            auth_h = headers.get("authorization", "") or headers.get("Authorization", "")
            caller_token_sha = _h_tk.sha256(auth_h.encode("utf-8")).hexdigest()[:12]
            try:
                b = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                b = {}
            agent_id = (b.get("agent_id") or "").strip() if isinstance(b, dict) else ""
            if not agent_id or not _re_tk.match(r"^[a-zA-Z0-9._\-]{1,128}$", agent_id):
                status = 400
                response_body = json.dumps({"error": "invalid_agent_id"})
            else:
                # 复用 read_pane capability: caller token 必须对该 agent 有 pane 读权
                cap_ok, cap_reason = _check_read_pane_capability(caller_token_sha, agent_id)
                if not cap_ok:
                    status = 403
                    response_body = json.dumps({
                        "error": "capability_denied", "detail": cap_reason,
                    })
                else:
                    try:
                        tk = _sse_ticket.issue(caller_token_sha, agent_id)
                        response_body = json.dumps({
                            "ticket": tk,
                            "ttl": _sse_ticket.TICKET_TTL,
                            "agent_id": agent_id,
                        })
                    except RuntimeError as e:
                        status = 429
                        response_body = json.dumps({
                            "error": "too_many_active_tickets",
                            "detail": str(e),
                        })
        elif path.startswith("/api/v1/runtime/process/"):
            # fn_runtime process_lifecycle 控制端
            # . POST /api/v1/runtime/process/{action} body {target_id, force?}
            # action ∈ {start, stop, restart, health}
            action = path[len("/api/v1/runtime/process/"):]
            if action not in {"start", "stop", "restart", "health"}:
                status = 400
                response_body = json.dumps({"error": "invalid action",
                                              "allowed": ["start", "stop", "restart", "health"]})
            else:
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except json.JSONDecodeError:
                    status = 400
                    response_body = json.dumps({"error": "bad json body"})
                else:
                    target_id = payload.get("target_id") or ""
                    force = bool(payload.get("force", False))
                    if not target_id:
                        status = 400
                        response_body = json.dumps({"error": "missing target_id"})
                    else:
                        # 路径限 [a-zA-Z0-9_\-] 防注入 / scope creep
                        import re as _re
                        if not _re.match(r"^[a-zA-Z0-9_\-]{1,64}$", target_id):
                            status = 400
                            response_body = json.dumps({"error": "invalid target_id format"})
                        else:
                            try:
                                _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                                if _here not in sys.path:
                                    sys.path.insert(0, _here)
                                from runtime.process_lifecycle import (
                                    health as _rt_health, start as _rt_start,
                                    stop as _rt_stop, restart as _rt_restart,
                                )
                                initiated_by = "master_api"
                                if action == "health":
                                    result = _rt_health(target_id)
                                elif action == "start":
                                    result = _rt_start(target_id, force=force, initiated_by=initiated_by)
                                elif action == "stop":
                                    result = _rt_stop(target_id, force=force, initiated_by=initiated_by)
                                elif action == "restart":
                                    result = _rt_restart(target_id, initiated_by=initiated_by)
                                response_body = json.dumps(result, ensure_ascii=False)
                                if not result.get("ok") and "missing_target" in str(result.get("error", "")):
                                    status = 404
                                elif not result.get("ok") and action != "health":
                                    status = 502
                            except ImportError as e:
                                status = 503
                                response_body = json.dumps({"error": "runtime module unavailable",
                                                              "detail": str(e)[:200]})
        elif path.startswith("/api/v1/runtime/conversation/"):
            # fn_runtime conversation_lifecycle 控制端
            # v1.1. POST /api/v1/runtime/conversation/{action} body {agent_id, force?}
            # action ∈ {clear, compact, evaluate, health}. 触发以任务为单位 (user ).
            action = path[len("/api/v1/runtime/conversation/"):]
            if action not in {"clear", "compact", "evaluate", "health"}:
                status = 400
                response_body = json.dumps({"error": "invalid action",
                                              "allowed": ["clear", "compact", "evaluate", "health"]})
            else:
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except json.JSONDecodeError:
                    status = 400
                    response_body = json.dumps({"error": "bad json body"})
                else:
                    agent_id = payload.get("agent_id") or ""
                    force = bool(payload.get("force", False))
                    mini_task = payload.get("mini_task") or None
                    if not agent_id:
                        status = 400
                        response_body = json.dumps({"error": "missing agent_id"})
                    else:
                        import re as _re
                        # agent_id 校验 (放宽到 . _ - 字符, 兼容 local.cli-claude-code-local.foo)
                        if not _re.match(r"^[a-zA-Z0-9._\-]{1,128}$", agent_id):
                            status = 400
                            response_body = json.dumps({"error": "invalid agent_id format"})
                        else:
                            try:
                                _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                                if _here not in sys.path:
                                    sys.path.insert(0, _here)
                                from runtime.conversation_lifecycle import (
                                    health as _cl_health, clear as _cl_clear,
                                    compact as _cl_compact, auto_evaluate as _cl_eval,
                                )
                                # phase 3: master 单线程 HTTPServer 嵌套 self-call
                                # 会 deadlock. endpoint 内直接拼 in-memory meta 注入,
                                # 避 conversation_lifecycle 内部走 HTTP 自调.
                                _meta = None
                                try:
                                    for _a in registry.list_agents():
                                        if _a.get("agent_id") == agent_id:
                                            _meta = dict(_a)
                                            _meta["activity"] = registry.get_activity(agent_id) or {}
                                            break
                                except (AttributeError, RuntimeError):
                                    _meta = None
                                initiated_by = payload.get("initiated_by") or "master_api"
                                if action == "health":
                                    result = _cl_health(agent_id, agent_meta_override=_meta)
                                elif action == "clear":
                                    parent_did = payload.get("parent_dispatch_id") or ""
                                    result = _cl_clear(agent_id, initiated_by=initiated_by,
                                                        force=force, parent_dispatch_id=parent_did,
                                                        agent_meta_override=_meta)
                                elif action == "compact":
                                    parent_did = payload.get("parent_dispatch_id") or ""
                                    result = _cl_compact(agent_id, initiated_by=initiated_by,
                                                          force=force, parent_dispatch_id=parent_did,
                                                          agent_meta_override=_meta)
                                elif action == "evaluate":
                                    result = _cl_eval(agent_id, mini_task=mini_task,
                                                       initiated_by=initiated_by,
                                                       agent_meta_override=_meta)
                                response_body = json.dumps(result, ensure_ascii=False)
                                if not result.get("ok") and action not in ("health", "evaluate"):
                                    status = 502 if "tmux" in str(result.get("error", "")) or "send" in str(result.get("error", "")) else 200
                                    if "in_cooldown" == result.get("error"):
                                        status = 429
                            except ImportError as e:
                                status = 503
                                response_body = json.dumps({"error": "conversation_lifecycle module unavailable",
                                                              "detail": str(e)[:200]})
        elif path == "/api/v1/admin/sync-rules":
            # phase 1: master broadcast 规则文件给所有 remote node
            # body: {target: "freerun/freerun_allowlist.json"}
            # 单向, HMAC + sha256 验证. 严禁 node→master 反向 push (M2 / agent-security)
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                target = payload.get("target") or ""
                # 路径限制 (防注入): 必须 freerun/*, 不含 ..
                if (".." in target or target.startswith("/")
                        or not target.startswith("freerun/")):
                    status = 400
                    response_body = json.dumps({"error": "invalid target", "target": target})
                else:
                    secret = os.environ.get("PRE_SYNC_HMAC_SECRET", "")
                    if not secret or len(secret) < 32:
                        status = 503
                        response_body = json.dumps({
                            "error": "PRE_SYNC_HMAC_SECRET missing or <32 bytes",
                            "hint": "set env: export PRE_SYNC_HMAC_SECRET=$(openssl rand -hex 32)",
                        })
                    else:
                        import hashlib as _hashlib
                        import hmac as _hmac
                        from pathlib import Path as _Path
                        rule_root = _Path(_PRE_RULE_ROOT)
                        src = rule_root / target
                        if not src.is_file():
                            status = 404
                            response_body = json.dumps({"error": "source file missing", "src": str(src)})
                        else:
                            try:
                                content = src.read_text(encoding="utf-8")
                            except OSError as e:
                                status = 500
                                response_body = json.dumps({"error": f"read failed: {e}"})
                            else:
                                sha256 = _hashlib.sha256(content.encode("utf-8")).hexdigest()
                                hmac_sig = _hmac.new(
                                    secret.encode("utf-8"),
                                    content.encode("utf-8"),
                                    _hashlib.sha256,
                                ).hexdigest()
                                rpc = {
                                    "jsonrpc": "2.0",
                                    "method": "sync_rules",
                                    "params": {
                                        "target_relpath": target,
                                        "content": content,
                                        "sha256": sha256,
                                        "hmac": hmac_sig,
                                    },
                                }
                                # broadcast: 跳过 local node (本机 fs 共享, master/node 同 disk)
                                broadcast_to = []
                                failed = []
                                for nid, n in list(registry.nodes.items()):
                                    if nid == "local":
                                        continue
                                    if not n.ws_writer:
                                        failed.append({"node_id": nid, "reason": "ws_offline"})
                                        continue
                                    try:
                                        await send_to_writer(n.ws_writer, json.dumps(rpc, ensure_ascii=False))
                                        broadcast_to.append(nid)
                                    except Exception as e:
                                        failed.append({"node_id": nid, "reason": str(e)[:100]})
                                response_body = json.dumps({
                                    "ok": True,
                                    "target": target,
                                    "bytes": len(content),
                                    "sha256": sha256,
                                    "broadcast_to": broadcast_to,
                                    "failed": failed,
                                })
        elif path.startswith("/api/v1/nodes/") and path.endswith("/rediscover"):
            node_id = path[len("/api/v1/nodes/"):-len("/rediscover")]
            ok, info = await trigger_node_rediscover(node_id, registry)
            if ok:
                response_body = json.dumps({"ok": True, **info})
            else:
                status = 404 if info.get("reason") == "node_not_found" else 502
                response_body = json.dumps({"ok": False, **info})
        elif path.startswith("/api/v1/dispatches/") and path.endswith("/export"):
            # 生成 task markdown 文档 → pre_log/tasks/{id}.md
            did = path[len("/api/v1/dispatches/"):-len("/export")]
            # 校验 did 安全 (防路径注入)
            import re as _re
            if not _re.match(r"^[A-Za-z0-9._-]{1,64}$", did):
                status = 400
                response_body = json.dumps({"error": "invalid dispatch_id"})
            else:
                events_raw = db.query_dispatch_events(did)
                if not events_raw:
                    status = 404
                    response_body = json.dumps({"error": "no events for dispatch_id", "dispatch_id": did})
                else:
                    disps = db.list_dispatches(since=0, limit=200)
                    meta = next((d for d in disps if d.get("dispatch_id") == did), {})
                    md_lines = [
                        f"# Task {did}",
                        "",
                        "## Meta",
                        "",
                        f"- **Status**: {meta.get('status', 'unknown')}",
                        f"- **CEO**: {meta.get('ceo') or '-'}",
                        f"- **Dispatcher**: {meta.get('dispatcher') or '-'}",
                        f"- **Executor**: {meta.get('executor') or '-'}",
                        f"- **Managers**: {', '.join(meta.get('managers') or []) or '-'}",
                        f"- **Started**: {meta.get('started_ts')}",
                        f"- **Last update**: {meta.get('last_ts')}",
                        f"- **Message count**: {meta.get('msg_count', len(events_raw))}",
                        "",
                    ]
                    if meta.get("brief"):
                        md_lines += [
                            "## Brief",
                            "",
                            meta["brief"],
                            "",
                        ]
                    if meta.get("task_title_sample"):
                        md_lines += [
                            "## Task Title (sample)",
                            "",
                            meta["task_title_sample"],
                            "",
                        ]
                    md_lines += ["## Timeline", ""]
                    for e in events_raw:
                        payload = e.get("payload") or {}
                        text = (payload.get("text") or payload.get("brief")
                                or payload.get("summary") or "") if isinstance(payload, dict) else ""
                        ts_iso = ""
                        try:
                            from datetime import datetime as _dt, timezone as _tz
                            ts_iso = _dt.fromtimestamp(e.get("ts", 0), tz=_tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        except (ValueError, OSError):
                            ts_iso = str(e.get("ts", ""))
                        md_lines += [
                            f"### {ts_iso} · {e.get('kind', '?')} · {e.get('from_agent') or '?'} → {e.get('to_agent') or '?'}",
                            "",
                            text[:1500] if text else "(no text payload)",
                            "",
                        ]
                    # final report (kind=report 末条)
                    report_evt = next((e for e in reversed(events_raw)
                                       if e.get("kind") == "report"), None)
                    if report_evt:
                        rp = report_evt.get("payload") or {}
                        rtext = rp.get("text") or rp.get("summary") or ""
                        if rtext:
                            md_lines += ["## Final Report", "", rtext, ""]
                    md_content = "\n".join(md_lines)
                    md_path = os.path.join(_PRE_LOG_ROOT, "tasks", f"{did}.md")
                    try:
                        os.makedirs(os.path.dirname(md_path), exist_ok=True)
                        with open(md_path, "w", encoding="utf-8") as f:
                            f.write(md_content)
                        try:
                            os.chmod(md_path, 0o600)
                        except OSError:
                            pass
                        response_body = json.dumps({
                            "ok": True,
                            "dispatch_id": did,
                            "path": md_path,
                            "size": len(md_content.encode("utf-8")),
                            "event_count": len(events_raw),
                        })
                    except OSError as e:
                        status = 500
                        response_body = json.dumps({"error": str(e)[:200]})
        elif path == "/api/v1/usage/snapshot":
            # (user ): account 升主键 + db 唯一 SoT.
            # 流程: parse → format validate → 入库 v2 表 (UPSERT WHERE excluded.fetch_ts > existing).
            # 不再写 in-memory registry.usage_by_node 作为查询源; legacy by_node 字段在 GET 派生.
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                node_id = payload.get("node_id") or "local"
                fetch_ts = payload.get("ts")
                _now = time.time()
                if not isinstance(fetch_ts, (int, float)) or fetch_ts <= 0:
                    fetch_ts = _now
                # 同时 in-memory by_node 也写一份 (legacy GUI/sys_beep 还在过渡), 但 GET path 优先 db
                old = (registry.usage_by_node.get(node_id) or {}) if node_id != "local" else (registry.usage or {})
                merged = {"probed_ts": fetch_ts, "node_id": node_id}
                v2_results = []  # 每个 provider 的入库 action
                _VALID_STATUS = ("ok", "limit_reached")
                _ALL_PROVIDERS = ("claude", "claude_agent-research", "gemini", "codex")
                for provider in _ALL_PROVIDERS:
                    if provider not in payload:
                        # legacy: 缺 key 保留 in-memory 旧值
                        if provider in old:
                            merged[provider] = old[provider]
                        continue
                    pdata = payload.get(provider) or {}
                    if not isinstance(pdata, dict):
                        v2_results.append({"provider": provider, "action": "rejected_not_dict"})
                        continue
                    pstatus = pdata.get("status")
                    # legacy in-memory sticky (兼容旧 caller, 渐进 deprecate)
                    incomplete = ("probe_inconclusive", "status_bar_only", "unknown", "error", "skipped")
                    if pstatus in incomplete and (old.get(provider) or {}).get("status") not in incomplete and old.get(provider):
                        merged[provider] = old[provider]
                    else:
                        merged[provider] = pdata
                    # v2 db: 仅 valid status 入库
                    if pstatus not in _VALID_STATUS:
                        v2_results.append({"provider": provider, "action": "skipped_invalid_status",
                                            "status": pstatus})
                        continue
                    account = pdata.get("account") or f"unknown@{node_id}"
                    used_pct = pdata.get("session_percent_used")
                    if used_pct is None:
                        used_pct = pdata.get("week_percent_used")
                    if used_pct is None:
                        # gemini models max 或 codex left → used
                        if isinstance(pdata.get("models"), dict):
                            pcts = [v.get("percent_used") for v in pdata["models"].values()
                                     if isinstance(v, dict) and isinstance(v.get("percent_used"), (int, float))]
                            if pcts:
                                used_pct = float(max(pcts))
                        elif isinstance(pdata.get("percent_left_5h"), (int, float)):
                            used_pct = 100.0 - float(pdata["percent_left_5h"])
                    reset_at = (pdata.get("session_reset") or pdata.get("reset_at")
                                or pdata.get("reset_5h") or pdata.get("week_reset"))
                    ok_db, action = db.upsert_usage_snapshot_v2(
                        provider=provider, account=account,
                        status=pstatus, used_pct=used_pct, reset_at=reset_at,
                        fetch_ts=fetch_ts, collected_by_node=node_id,
                        parsed=pdata, raw_excerpt=pdata.get("raw_excerpt") or "",
                    )
                    v2_results.append({"provider": provider, "account": account,
                                        "ok": ok_db, "action": action})
                # severity 透传 (legacy)
                if "severity" in payload:
                    merged["severity"] = payload["severity"]
                registry.usage_by_node[node_id] = merged
                if node_id == "local":
                    registry.usage = merged
                # audit (kind=usage_snapshot) 入 db 但不 forward
                import uuid
                msg_dict = {
                    "id": uuid.uuid4().hex, "ts": time.time(),
                    "from_agent": "pre.usage_probe", "to_agent": "audit.usage",
                    "from_role": "platform", "to_role": "audit",
                    "kind": "usage_snapshot",
                    "payload": {"ts": payload.get("ts"),
                                "severity": payload.get("severity"),
                                "node_id": node_id,
                                "v2_results": v2_results,
                                "providers": list(payload.keys() & set(_ALL_PROVIDERS))},
                    "parent_id": None, "priority": 0,
                }
                try:
                    db.insert_message(msg_dict)
                except Exception as e:
                    print(f"[master] usage_snapshot insert failed: {e}", flush=True)
                response_body = json.dumps({
                    "ok": True, "msg_id": msg_dict["id"],
                    "node_id": node_id,
                    "v2_results": v2_results,
                })
        elif path == "/api/v1/usage/external":
            # + (user ): 外部 API 输入端点.
            # 用于 cli pane 抓不到的 provider (apikey claude_agent-research / kimi / 自定义模型 etc),
            # 由调用方 (业务进程 / 第三方脚本) 直接 POST 标准 schema, 跳过 cli probe.
            # schema (必需): {provider, account, status, fetch_ts}
            # 可选: {used_pct, reset_at, collected_by_node, parsed, raw_excerpt}
            # validate: status ∈ {ok, limit_reached}; 否则 400
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                _required = ("provider", "account", "status", "fetch_ts")
                _missing = [k for k in _required if k not in payload]
                if _missing:
                    status = 400
                    response_body = json.dumps({"error": "missing fields",
                                                  "missing": _missing,
                                                  "required": list(_required)})
                elif payload.get("status") not in ("ok", "limit_reached"):
                    status = 400
                    response_body = json.dumps({
                        "error": "invalid status",
                        "got": payload.get("status"),
                        "allowed": ["ok", "limit_reached"],
                    })
                elif not isinstance(payload.get("fetch_ts"), (int, float)) or payload["fetch_ts"] <= 0:
                    status = 400
                    response_body = json.dumps({"error": "invalid fetch_ts (must be positive number)"})
                else:
                    ok_db, action = db.upsert_usage_snapshot_v2(
                        provider=payload["provider"],
                        account=payload["account"],
                        status=payload["status"],
                        used_pct=payload.get("used_pct"),
                        reset_at=payload.get("reset_at"),
                        fetch_ts=float(payload["fetch_ts"]),
                        collected_by_node=payload.get("collected_by_node") or "external",
                        parsed=payload.get("parsed") or {},
                        raw_excerpt=payload.get("raw_excerpt") or "",
                    )
                    # audit
                    import uuid
                    msg_dict = {
                        "id": uuid.uuid4().hex, "ts": time.time(),
                        "from_agent": "external.usage_input",
                        "to_agent": "audit.usage",
                        "from_role": "platform", "to_role": "audit",
                        "kind": "usage_snapshot",
                        "payload": {
                            "ts": payload["fetch_ts"],
                            "node_id": payload.get("collected_by_node") or "external",
                            "v2_results": [{"provider": payload["provider"],
                                            "account": payload["account"],
                                            "ok": ok_db, "action": action}],
                            "providers": [payload["provider"]],
                            "source": "external_api",
                        },
                        "parent_id": None, "priority": 0,
                    }
                    try:
                        db.insert_message(msg_dict)
                    except Exception as e:
                        print(f"[master] usage_external audit insert failed: {e}", flush=True)
                    response_body = json.dumps({
                        "ok": ok_db, "action": action,
                        "msg_id": msg_dict["id"],
                        "provider": payload["provider"],
                        "account": payload["account"],
                    })
        elif path == "/api/v1/usage/event":
            # usage_probe_once severity 变化时 push, audit + 后续推 fn_ops_account
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                import uuid
                msg_dict = {
                    "id": uuid.uuid4().hex, "ts": payload.get("ts") or time.time(),
                    "from_agent": "pre.usage_probe", "to_agent": "audit.usage",
                    "from_role": "platform", "to_role": "audit",
                    "kind": "usage_event",
                    "payload": {
                        "provider": payload.get("provider"),
                        "severity": payload.get("severity"),
                        "prev_severity": payload.get("prev_severity"),
                        "used_summary": payload.get("used_summary"),
                    },
                    "parent_id": None, "priority": 0,
                }
                try:
                    db.insert_message(msg_dict)
                except Exception as e:
                    print(f"[master] usage_event insert failed: {e}", flush=True)
                # 后续 phase 2: 推 fn_ops_account chat (kind=usage_event), critical 推 user.default
                # 当前 phase 1 仅留档 (fn_ops_account 未 spawn, user.default virtual agent 是 )
                response_body = json.dumps({
                    "ok": True, "msg_id": msg_dict["id"],
                    "audit_only": True,
                    "broadcast_to": [],  # phase 2 填 fn_ops_account / user.default
                })
        elif path == "/api/v1/files/upload":
            # 跨 node 文件上传, master 自存
            import hashlib
            import uuid as _uuid
            owner = headers.get("x-agent-id", "") or "?"
            recipient = headers.get("x-recipient", "") or ""
            file_name = headers.get("x-file-name", "") or "unnamed"
            content_length = int(headers.get("content-length", "0") or "0")
            if content_length > MAX_FILE_SIZE:
                status = 413
                response_body = json.dumps({"error": "file_too_large",
                                            "max_size": MAX_FILE_SIZE,
                                            "got": content_length})
            elif content_length <= 0:
                status = 400
                response_body = json.dumps({"error": "empty_body_or_no_content_length"})
            elif not body:
                status = 400
                response_body = json.dumps({"error": "no_body"})
            else:
                allowed, rl_reason = _file_rate_check(owner, "upload")
                if not allowed:
                    status = 429
                    response_body = json.dumps({"error": "rate_limited",
                                                "reason": rl_reason})
                else:
                    file_id = _uuid.uuid4().hex[:16]
                    sha256 = hashlib.sha256(body).hexdigest()
                    fp = _file_path(owner, file_id, file_name)
                    try:
                        with open(fp, "wb") as f:
                            f.write(body)
                        try:
                            os.chmod(fp, 0o600)
                        except OSError:
                            pass
                        _FILE_META[file_id] = {
                            "owner": owner,
                            "recipient": recipient,
                            "ts": time.time(),
                            "name": _sanitize_name(file_name),
                            "size": len(body),
                            "sha256": sha256,
                            "path": fp,
                        }
                        _audit_file({
                            "ts": time.time(),
                            "op": "upload",
                            "agent_id": owner,
                            "recipient": recipient,
                            "file_id": file_id,
                            "name": _sanitize_name(file_name),
                            "size": len(body),
                            "sha256": sha256,
                            "status": "ok",
                        })
                        response_body = json.dumps({
                            "ok": True,
                            "file_id": file_id,
                            "size": len(body),
                            "sha256": sha256,
                            "expires_ts": time.time() + FILE_RETENTION_DAYS * 86400,
                        })
                    except OSError as e:
                        status = 500
                        response_body = json.dumps({"error": str(e)[:200]})
        elif path == "/api/v1/cron/trigger":
            # cron daemon 触发 audit, 仅 db.insert_message 不 forward
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                import uuid
                msg_dict = {
                    "id": payload.get("id") or uuid.uuid4().hex,
                    "ts": payload.get("ts") or time.time(),
                    "from_agent": payload.get("from_agent", "cron.daemon"),
                    "to_agent": payload.get("to_agent", "audit.cron"),
                    "from_role": payload.get("from_role", "platform"),
                    "to_role": payload.get("to_role", "audit"),
                    "kind": "cron_trigger",
                    "payload": payload.get("payload", {}),
                    "parent_id": payload.get("parent_id"),
                    "priority": payload.get("priority", 0),
                }
                try:
                    db.insert_message(msg_dict)
                    response_body = json.dumps({"ok": True, "msg_id": msg_dict["id"]})
                except Exception as e:
                    status = 500
                    response_body = json.dumps({"ok": False, "error": str(e)[:200]})
        elif path.startswith("/api/v1/agents/") and path.endswith("/decide"):
            agent_id = path[len("/api/v1/agents/"):-len("/decide")]
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                key = payload.get("key", "")
                if not key:
                    status = 400
                    response_body = json.dumps({"error": "missing 'key'"})
                else:
                    ok, info = await forward_decide_to_agent(agent_id, key, registry, db,
                                                              by_agent=payload.get("by_agent", "master.api"))
                    if ok:
                        response_body = json.dumps({"ok": True, **info})
                    else:
                        status = 404 if info.get("reason") == "agent_not_found" else 502
                        response_body = json.dumps({"ok": False, **info})
        elif path.startswith("/api/v1/agents/") and path.endswith("/proposals"):
            # POST proposals (analyzer 写入)
            agent_id = path[len("/api/v1/agents/"):-len("/proposals")]
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                proposals = payload.get("proposals") or []
                if not isinstance(proposals, list):
                    status = 400
                    response_body = json.dumps({"error": "proposals must be array"})
                else:
                    registry.set_proposals(agent_id, proposals[:5])  # 最多 5 条
                    response_body = json.dumps({"ok": True, "count": len(proposals[:5])})
        elif path.startswith("/api/v1/agents/") and path.endswith("/choose-proposal"):
            # 用户选了 proposal, master 注入 agent + 清除 proposals + audit
            agent_id = path[len("/api/v1/agents/"):-len("/choose-proposal")]
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                proposal_id = payload.get("proposal_id", "")
                p_entry = registry.get_proposals(agent_id) or {}
                proposals = p_entry.get("proposals", [])
                chosen = next((p for p in proposals if p.get("id") == proposal_id), None)
                if not chosen:
                    status = 404
                    response_body = json.dumps({"error": "proposal_not_found",
                                                "proposal_id": proposal_id})
                else:
                    # 用 send_to_agent 路径注入 (kind=command, payload.text)
                    send_body = {
                        "kind": "proposal_chosen",
                        "from_agent": payload.get("by_agent", "user.default"),
                        "from_role": "user",
                        "payload": {
                            "text": chosen["text"],
                            "title": chosen.get("title", ""),
                            "proposal_id": proposal_id,
                        },
                    }
                    ok, info = await forward_send_to_agent(agent_id, send_body, registry, db)
                    registry.clear_proposals(agent_id)  # 一次性清除
                    if ok:
                        response_body = json.dumps({"ok": True, "title": chosen.get("title"), **info})
                    else:
                        status = 502
                        response_body = json.dumps({"ok": False, **info})
        elif path.startswith("/api/v1/agents/") and path.endswith("/task-summary"):
            # stop_analyzer 推 20 字 LLM 短语 (event-driven 替代 polling)
            agent_id = path[len("/api/v1/agents/"):-len("/task-summary")]
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                summary = (payload.get("summary") or "").strip()[:30]
                if summary:
                    registry.set_task_summary(agent_id, summary)
                    response_body = json.dumps({"ok": True, "summary": summary})
                else:
                    status = 400
                    response_body = json.dumps({"error": "empty summary"})
        elif path.startswith("/api/v1/agents/") and path.endswith("/mini-task"):
            # stop_analyzer 推 mini_task (transcript 解析末 cycle)
            agent_id = path[len("/api/v1/agents/"):-len("/mini-task")]
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                # 必填字段校验
                required = ("mini_task_id", "agent_id", "request",
                             "started_ts", "ended_ts")
                missing = [k for k in required if not payload.get(k)
                            and payload.get(k) != 0]
                if missing:
                    status = 400
                    response_body = json.dumps(
                        {"error": "missing fields", "missing": missing})
                elif payload.get("agent_id") != agent_id:
                    status = 400
                    response_body = json.dumps(
                        {"error": "agent_id mismatch path vs body"})
                else:
                    # 大小限制 (防 abuse): request + reply 超 200KB 拒
                    req_len = len(payload.get("request") or "")
                    reply_len = len(payload.get("reply") or "")
                    if req_len + reply_len > 200000:
                        status = 413
                        response_body = json.dumps(
                            {"error": "payload too large",
                             "request_len": req_len, "reply_len": reply_len})
                    else:
                        result = db.insert_mini_task(payload)
                        response_body = json.dumps({
                            **result,
                            "mini_task_id": payload["mini_task_id"],
                        })
        elif path.startswith("/api/v1/agents/") and path.endswith("/dismiss-proposals"):
            # 用户跳过, 清除 proposals
            # 默认 mute=true (防循环), body 可传 mute=false 仅清一次
            agent_id = path[len("/api/v1/agents/"):-len("/dismiss-proposals")]
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                payload = {}
            mute = payload.get("mute", True)  # default true 防循环
            registry.clear_proposals(agent_id)
            if mute:
                registry.mute_proposals(agent_id)
            response_body = json.dumps({"ok": True, "muted": mute})
        elif path.startswith("/api/v1/agents/") and path.endswith("/enable-proposals"):
            # 用户解除 mute, agent 下次 stop 重新生成
            agent_id = path[len("/api/v1/agents/"):-len("/enable-proposals")]
            registry.unmute_proposals(agent_id)
            response_body = json.dumps({"ok": True, "muted": False})
        elif path.startswith("/api/v1/user-decisions/") and \
                (path.endswith("/resolve") or path.endswith("/dismiss")):
            # user_decisions: POST /api/v1/user-decisions/{id}/resolve | /dismiss
            # body (resolve): {decision: "...", note?: "..."}
            # body (dismiss): {} or {note?: "..."}
            is_dismiss = path.endswith("/dismiss")
            suffix = "/dismiss" if is_dismiss else "/resolve"
            decision_id = path[len("/api/v1/user-decisions/"):-len(suffix)]
            if not decision_id or "/" in decision_id:
                status = 400
                response_body = json.dumps({"error": "invalid decision_id"})
            else:
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except json.JSONDecodeError:
                    status = 400
                    response_body = json.dumps({"error": "bad json body"})
                else:
                    try:
                        _here_src = os.path.dirname(os.path.dirname(
                            os.path.abspath(__file__)))
                        if _here_src not in sys.path:
                            sys.path.insert(0, _here_src)
                        if is_dismiss:
                            from user_decisions import dismiss_decision as _ud_op
                            ok_op = _ud_op(decision_id)
                        else:
                            from user_decisions import resolve_decision as _ud_op
                            decision_text = payload.get("decision", "")
                            note_text = payload.get("note", "")
                            if not decision_text:
                                status = 400
                                response_body = json.dumps({
                                    "error": "missing decision field"})
                                ok_op = None
                            else:
                                ok_op = _ud_op(decision_id, decision_text, note_text)
                        if ok_op is None:
                            pass  # already responded
                        elif ok_op:
                            response_body = json.dumps({
                                "ok": True, "decision_id": decision_id,
                                "action": "dismiss" if is_dismiss else "resolve"})
                        else:
                            status = 404
                            response_body = json.dumps({
                                "ok": False,
                                "error": "decision not found or write failed",
                                "decision_id": decision_id})
                    except ImportError as e:
                        status = 503
                        response_body = json.dumps({
                            "error": "user_decisions module unavailable",
                            "detail": str(e)[:200]})
        elif path.startswith("/api/v1/agents/") and path.endswith("/send"):
            agent_id = path[len("/api/v1/agents/"):-len("/send")]
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                status = 400
                response_body = json.dumps({"error": "bad json body"})
            else:
                # M7-3: 注入 X-FN-Node-Id header 作 _validated_source_node
                # forward_send_to_agent 看此字段做二次校 (mcp_tool_call kind)
                _src = headers.get("x-fn-node-id") or ""
                if _src:
                    payload["_validated_source_node"] = _src
                ok, info = await forward_send_to_agent(agent_id, payload, registry, db)
                if ok:
                    response_body = json.dumps({"ok": True, "queued": True, **info})
                else:
                    status = 404 if info.get("reason") == "agent_not_found" else 502
                    response_body = json.dumps({"ok": False, **info})
        else:
            status = 501
            response_body = json.dumps({"error": "not implemented"})
    else:
        status = 405
        response_body = json.dumps({"error": "method not allowed"})

    body_bytes = response_body.encode("utf-8")
    status_line = {200: "200 OK", 400: "400 Bad Request", 404: "404 Not Found",
                   405: "405 Method Not Allowed", 501: "501 Not Implemented",
                   502: "502 Bad Gateway"}.get(status, f"{status} ?")
    response = (
        f"HTTP/1.1 {status_line}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body_bytes
    writer.write(response)
    await writer.drain()
    writer.close()


# ---------- WS handler (Node) ----------

async def handle_node_ws(reader, writer, registry, db, secret: str):
    """
    处理 /node WS 连接.
    协议: JSON-RPC 2.0 over WS text frame
    第一帧必须是 register_node (含 secret)
    """
    peer = writer.get_extra_info("peername")
    peer_addr = f"{peer[0]}:{peer[1]}" if peer else "?"
    node_id: Optional[str] = None

    try:
        # 等第一帧 register_node
        opcode, payload = await read_frame(reader, expect_masked=True)
        if opcode == OPCODE_CLOSE:
            return
        if opcode != OPCODE_TEXT:
            await send_close(writer)
            return

        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            await send_close(writer)
            return

        if msg.get("method") != "register_node":
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32601, "message": "expected register_node first"}
            }))
            await send_close(writer)
            return

        params = msg.get("params", {})
        # multi-token RBAC: register_node frame 的 secret 必须是 role=node 的 token
        from master.auth import verify_token as _verify_token
        _claimed_secret = params.get("secret", "")
        _ok_n, _reason_n, _ctx_n = _verify_token(
            db, _claimed_secret,
            required_scope="bus.connect",
            expected_role="node",
        )
        if not _ok_n:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32000,
                          "message": f"bad node token ({_reason_n})"}
            }))
            await send_close(writer)
            return

        node_id = params.get("node_id")
        if not node_id:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32602, "message": "missing node_id"}
            }))
            await send_close(writer)
            return

        info = NodeInfo(
            node_id=node_id,
            host=params.get("host", peer_addr),
            capabilities=params.get("capabilities", []),
            ws_writer=writer,
        )
        registry.add_node(info)
        print(f"[master] node registered: {node_id} from {peer_addr}", flush=True)

        # ack
        await send_text(writer, json.dumps({
            "jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"ok": True, "ts": time.time()}
        }))

        # 后续消息循环
        while True:
            opcode, payload = await read_frame(reader, expect_masked=True)
            if opcode == OPCODE_CLOSE:
                break
            if opcode == OPCODE_PING:
                # 回 pong
                writer.write(encode_frame(payload, OPCODE_PONG, masked=False))
                await writer.drain()
                continue
            if opcode != OPCODE_TEXT:
                continue

            try:
                m = json.loads(payload.decode("utf-8"))
            except Exception:
                continue

            await dispatch_node_message(m, node_id, registry, db, writer)

    except (ConnectionError, asyncio.IncompleteReadError, BrokenPipeError) as e:
        print(f"[master] node {node_id or peer_addr} disconnected: {e}", flush=True)
    finally:
        if node_id:
            registry.remove_node(node_id)
            print(f"[master] node unregistered: {node_id}", flush=True)
        try:
            writer.close()
        except Exception:
            pass


async def dispatch_node_message(m: dict, node_id: str, registry: Registry,
                                db: MasterDB, writer):
    """处理 Node 通过 WS 发来的 JSON-RPC 消息"""
    method = m.get("method")
    params = m.get("params", {})
    msg_id = m.get("id")

    if method == "node_heartbeat":
        registry.touch_node(node_id)
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"ok": True, "ts": time.time()}
            }))

    elif method == "register_agent":
        # G1: agent_id 前缀强校验
        # 4 层防御 (_validate_agent_id_for_node): _FORBIDDEN_CTRL + 正则 + 长度 + 前缀 = node_id
        # 已知风险点 CLAUDE.md §3 反向入侵防御 #2/#3 修补
        _aid = params.get("agent_id")
        _ok, _reason = _validate_agent_id_for_node(_aid, node_id)
        if not _ok:
            print(f"[master] REJECTED register_agent {_aid!r} from node {node_id!r}: {_reason}",
                  flush=True)
            if msg_id is not None:
                await send_text(writer, json.dumps({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32100, "message": f"agent_id rejected: {_reason}"}
                }))
            return
        info = AgentInfo(
            agent_id=_aid,
            node_id=node_id,
            driver_type=params.get("driver_type", ""),
            role=params.get("role", "worker"),
            state=params.get("state", "idle"),
            capabilities=params.get("capabilities", []),
            metadata=params.get("metadata", {}),
        )
        registry.upsert_agent(info)
        print(f"[master] agent registered: {info.agent_id} (role={info.role})", flush=True)
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}
            }))

    elif method == "unregister_agent":
        registry.remove_agent(params.get("agent_id"))
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}
            }))

    elif method == "agent_state":
        registry.update_agent_state(params.get("agent_id"), params.get("state"))
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}
            }))

    elif method == "report_pending":
        # node 推上来的 pending 列表 (该 node 全量)
        pending_list = params.get("pending", []) or []
        registry.replace_pending_for_node(node_id, pending_list)
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}
            }))

    elif method == "report_activity":
        # node 推上来的 agent activity (state/last_action/pane_summary)
        activity_list = params.get("activity", []) or []
        registry.replace_activity_for_node(node_id, activity_list)
        # decide 重试 — 按 pane_fp 字节指纹判定, blocked_user 且 fp 未变才重发
        try:
            await _process_pending_decides_on_activity(activity_list, registry)
        except Exception as e:
            print(f"[master] _process_pending_decides_on_activity error: {e}", flush=True)
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}
            }))

    elif method == "report_usage":
        # Phase A
        # node→master 反向 telemetry push. 4 层防御 (G2) + master_post_recv 脱敏 (G3) + fail-closed (G5).
        # [remote-node+local-only hack 自 待 ≥3 node 升级通用 registry, 见 G10]
        _ok, _reason, _redacted, _redact_hits, _payload_size = \
            _validate_telemetry_payload(node_id, params)
        if not _ok:
            _audit_telemetry(node_id, "rejected", _reason, _payload_size, _redact_hits or {})
            print(f"[master] REJECTED report_usage from node {node_id!r}: {_reason}",
                  flush=True)
            # G5 60s 单 node ≥3 reject 触发 agent-security alert ( 限频)
            _check_telemetry_reject_burst(node_id)
            if msg_id is not None:
                await send_text(writer, json.dumps({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32100,
                              "message": f"telemetry rejected: {_reason}"}
                }))
            return
        # G4 lazy stale 检测 (last_seen > 120s → finding HIGH + alert)
        _check_collector_stale(node_id, registry)
        #         # IC-2=C lock = Phase A 零新存储 (registry.usage_by_node + messages.kind=usage_event 双轨),
        # SQLite usage_telemetry 表降为 Phase B advisory 蓝图.
        # HC-G4 痕迹保留: usage_telemetry write 路径下面注释保留不删, 等 Phase B 真长期趋势分析需求出现再启.
        try:
            now_ts = time.time()
            row = dict(_redacted)
            row["recv_ts"] = now_ts
            row["redact_hits"] = _redact_hits or {}
            # [Phase B advisory blueprint per user IC-2=C lock,
            # write path commented per dispatcher 18:35 ruling, ]
            # new_id = db.insert_usage_telemetry(row) # 回滚: IC-2=C 严守, SQLite Phase B advisory
            new_id = None  # IC-2=C 双轨不入 SQLite
            # IC-2=C 双轨核心 1: messages.kind=usage_event audit insert (替代 SQLite write)
            try:
                import uuid as _uuid_audit
                _msg_dict = {
                    "id": _uuid_audit.uuid4().hex,
                    "ts": now_ts,
                    "from_agent": f"{node_id}.collector.usage",
                    "to_agent": "audit.usage",
                    "from_role": "platform",
                    "to_role": "audit",
                    "kind": "usage_event",
                    "payload": {
                        "schema_version": row.get("schema_version", "v1"),
                        "node_id": node_id,
                        "cli_type": row.get("cli_type"),
                        "ts": row.get("ts"),
                        "recv_ts": now_ts,
                        "agent_id": row.get("agent_id"),
                        "session_id": row.get("session_id"),
                        "model": row.get("model"),
                        "token_input": row.get("token_input"),
                        "token_output": row.get("token_output"),
                        "token_total": row.get("token_total"),
                        "quota_used": row.get("quota_used"),
                        "quota_limit": row.get("quota_limit"),
                        "quota_used_pct": row.get("quota_used_pct"),
                        "quota_reset_at": row.get("quota_reset_at"),
                        "billing_period": row.get("billing_period"),
                        "project_name": row.get("project_name"),
                        "redact_hits": row.get("redact_hits") or {},
                    },
                    "parent_id": None,
                    "priority": 0,
                }
                db.insert_message(_msg_dict)
                new_id = _msg_dict["id"]  # 用 messages msg_id 替代原 SQLite row_id
            except Exception as e:
                _audit_telemetry(node_id, "rejected",
                                 f"messages_insert_error:{type(e).__name__}",
                                 _payload_size, _redact_hits or {})
                if msg_id is not None:
                    await send_text(writer, json.dumps({
                        "jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32603, "message": "internal messages db error"}
                    }))
                return
            # Phase A v2 (HC-DRLI-1/2/4 + M11 outbox pattern):
            # outbox 实施序: 1 messages.audit (✓ 上面已写) → 2 last_success_per_node DB SOT UPSERT → 3 派生 registry
            # success/fail 双路径:
            # - success: UPSERT (全字段) + registry update
            # - fail: UPSERT (仅 status+ts_last_attempt 更新, ts_last_success+业务字段保留上次) + 不动 registry
            _status_attempt = row.get("status") or "success"  # default success backward compat (但 v2 schema 必填)
            _ok_db_upsert = db.upsert_last_success(
                node_id=node_id,
                cli_type=row.get("cli_type") or "unknown",
                status_last_attempt=_status_attempt,
                ts_last_attempt=row.get("ts") or now_ts,
                recv_ts=now_ts,
                # 业务字段 (success 时全 update, fail 时 SQL 内不动这些列)
                quota_used=row.get("quota_used"),
                quota_limit=row.get("quota_limit"),
                quota_used_pct=row.get("quota_used_pct"),
                quota_reset_at=row.get("quota_reset_at"),
                billing_period=row.get("billing_period"),
                model=row.get("model"),
                agent_id=row.get("agent_id"),
                session_id=row.get("session_id"),
                raw_excerpt=row.get("raw_excerpt"),
            )
            if not _ok_db_upsert:
                # M11 agent-security 严禁 silent merge stale registry, DB 写失败 reject + finding HIGH + alert
                _audit_telemetry(node_id, "rejected",
                                 "last_success_upsert_failed",
                                 _payload_size, _redact_hits or {})
                # finding HIGH-last-success-upsert-failed
                try:
                    from pathlib import Path as _Path_h
                    _findings_dir = _Path_h(_PRE_LOG_ROOT) / "findings"
                    _findings_dir.mkdir(parents=True, exist_ok=True)
                    from datetime import datetime as _dt_h, timezone as _tz_h
                    _ts_h = _dt_h.now(tz=_tz_h.utc).strftime("%Y%m%dT%H%M%SZ")
                    _fpath_h = _findings_dir / f"HIGH-last-success-upsert-failed-{node_id}-{_ts_h}.md"
                    with open(_fpath_h, "w") as _fh:
                        _fh.write(
                            f"# HIGH: last_success_per_node UPSERT failed\n\n"
                            f"- ts: {_ts_h}\n"
                            f"- node_id: {node_id}\n"
                            f"- cli_type: {row.get('cli_type')}\n"
                            f"- status_attempt: {_status_attempt}\n"
                            f"- M11 outbox pattern: DB SOT 写失败必 reject\n"
                            f"- 严禁 silent merge stale registry\n\n"
                            f"<phase_a_v2 M11 agent-security>\n"
                        )
                    try:
                        os.chmod(str(_fpath_h), 0o600)
                    except OSError:
                        pass
                except OSError:
                    pass
                if msg_id is not None:
                    await send_text(writer, json.dumps({
                        "jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32603, "message": "last_success_upsert_failed"}
                    }))
                return
            # outbox 步 3: 派生 registry — success 路径才更新, fail 不动 (HC-DRLI-1 防 fail 覆盖)
            if _status_attempt == "success":
                try:
                    _node_usage = registry.usage_by_node.setdefault(node_id, {})
                    _cli = row.get("cli_type") or "unknown"
                    _node_usage[_cli] = {
                        "quota_used_pct": row.get("quota_used_pct"),
                        "quota_used": row.get("quota_used"),
                        "quota_limit": row.get("quota_limit"),
                        "quota_reset_at": row.get("quota_reset_at"),
                        "probed_ts": now_ts,
                        "source": "report_usage",
                    }
                except (AttributeError, TypeError):
                    pass
            # else fail: registry 保留上次 success 不动 (HC-DRLI-1 防覆盖)
        except Exception as e:
            _audit_telemetry(node_id, "rejected", f"telemetry_handler_error:{type(e).__name__}",
                             _payload_size, _redact_hits or {})
            if msg_id is not None:
                await send_text(writer, json.dumps({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": "internal handler error"}
                }))
            return
        # Phase A v2: audit 标 success/fail 区分
        _accept_decision = ("accepted_success" if _status_attempt == "success"
                              else "accepted_fail_audit_only")
        _audit_telemetry(node_id, _accept_decision, "", _payload_size, _redact_hits or {},
                         row_id=new_id)
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "ok": True,
                    "msg_id": new_id,
                    "status": _status_attempt,
                    "sot": ("messages+last_success_per_node+registry"
                              if _status_attempt == "success"
                              else "messages+last_success_attempt_only"),
                }
            }))

    elif method == "capture_pane":
        # — master→node ws RPC capture_pane
        # 跟 outbound_message 同方向 (node→master 反向调). master 派给 node 自己跑.
        # node 端: tmux_helper.capture_pane(session, lines, timeout) → 返 raw text.
        # 严禁 master 主动 ssh.
        # NOTE: 当前 master 端 endpoint /api/v1/agents/{id}/pane 走 target_node==local 同进程路径,
        # 远端 ws RPC 派需 master 持 node-side ws connection 反向 send_text + 等 response.
        # Phase 1 仅注册此 handler 占位 (跟 collector_heartbeat 同模式), node 端 driver 调用此
        # method 名 send 到 master 才会进 dispatch. 真双向 RPC ack 在 phase 2 跟 driver 联动.
        session = (params or {}).get("session", "")
        lines = int((params or {}).get("lines", 200) or 200)
        timeout = float((params or {}).get("timeout", 5.0) or 5.0)
        try:
            _here_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _here_src not in sys.path:
                sys.path.insert(0, _here_src)
            from tmux_helper import capture_pane as _cap, has_session as _has
            if not _has(session, timeout=2.0):
                _result = {"ok": False, "error": "session_not_found", "session": session}
            else:
                _raw = _cap(session, lines=lines, timeout=timeout)
                _result = {"ok": True, "session": session,
                            "content": _raw, "lines_returned": _raw.count("\n") + 1}
        except (ImportError, Exception) as e:  # noqa: BLE001
            _result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id, "result": _result
            }))

    elif method == "collector_heartbeat":
        # Phase A: collector 60s 心跳
        # HC-A9 0 LLM cost 例外. master 仅更新 last_seen, 不入 DB (heartbeat 频率 ≠ trend 数据).
        try:
            registry.collector_last_seen[node_id] = time.time()
        except (AttributeError, TypeError):
            try:
                registry.collector_last_seen = {node_id: time.time()}
            except AttributeError:
                pass
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}
            }))

    elif method == "outbound_message":
        # agent → master, 持久化, 后续路由 (driver/role phase 实现)
        # G1: from_agent 前缀强校验
        # 修补 CLAUDE.md §3 反向入侵防御 #2 (outbound_message.from_agent 不校验)
        msg_dict = params.get("message", {})
        _from = msg_dict.get("from_agent") if isinstance(msg_dict, dict) else None
        _ok, _reason = _validate_agent_id_for_node(_from, node_id)
        if not _ok:
            print(f"[master] REJECTED outbound_message from {_from!r} on node {node_id!r}: "
                  f"{_reason}", flush=True)
            if msg_id is not None:
                await send_text(writer, json.dumps({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32100,
                              "message": f"from_agent rejected: {_reason}"}
                }))
            return
        if msg_dict.get("id"):
            db.insert_message(msg_dict)
            # MVP 暂不路由, 仅持久化
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id, "result": {"ok": True}
            }))

    else:
        if msg_id is not None:
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"}
            }))


# ---------- 主连接 dispatcher ----------

async def handle_client(reader, writer, registry, db, secret):
    """读 HTTP 请求行+headers, 决定走 HTTP 还是 WS Upgrade"""
    try:
        # 读直到 \r\n\r\n
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                writer.close()
                return
            buf += chunk
            if len(buf) > 65536:
                writer.close()
                return

        head, _, rest = buf.partition(b"\r\n\r\n")
        method, path, headers = parse_http_request(head)
        body = rest
        # POST 等带 body 的, 按 Content-Length 读完
        clen_str = headers.get("content-length", "")
        if clen_str.isdigit():
            clen = int(clen_str)
            while len(body) < clen:
                more = await reader.read(min(4096, clen - len(body)))
                if not more:
                    break
                body += more
            body = body[:clen]

        # 解析 query (auth check 用; handle_http 也会再解一次)
        if "?" in path:
            path_only, _, qs = path.partition("?")
            query = {}
            for kv in qs.split("&"):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    query[k] = v
        else:
            path_only, query = path, {}

        # auth 检查 (HTTP + WS 都走) — multi-token RBAC, secret 参数已废弃
        # PR2: 取 source IP 让 _check_auth 做来源差异化 (mcp/hook 必 loopback)
        _peer = writer.get_extra_info("peername")
        _source_ip = _peer[0] if _peer and len(_peer) >= 1 else ""
        ok, reason, auth_ctx = _check_auth(method, path_only, headers, query, db, body,
                                            source_ip=_source_ip)
        if not ok:
            status_line, msg = ("401 Unauthorized", "auth_required")
            if reason.startswith("origin_not_allowed"):
                status_line, msg = "403 Forbidden", reason
            elif reason.startswith("scope_denied") or reason.startswith("role_mismatch") \
                 or reason.startswith("mcp_from_agent_mismatch") \
                 or reason.startswith("mcp_role_remote_ip_denied") \
                 or reason.startswith("hook_role_remote_ip_denied"):
                status_line, msg = "403 Forbidden", reason
            elif reason == "missing_or_bad_bearer":
                msg = reason
            err_body = json.dumps({"error": msg}).encode("utf-8")
            err = (
                f"HTTP/1.1 {status_line}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(err_body)}\r\n"
                "WWW-Authenticate: Bearer realm=\"pre\"\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii") + err_body
            writer.write(err)
            await writer.drain()
            writer.close()
            return

        # Upgrade: websocket + Connection: Upgrade (不是 "websocket")
        if headers.get("upgrade", "").lower() == "websocket":
            client_key = headers.get("sec-websocket-key", "")
            if not client_key:
                writer.close()
                return
            writer.write(build_handshake_response(client_key))
            await writer.drain()

            if path_only == "/node":
                await handle_node_ws(reader, writer, registry, db, secret)
            elif path_only == "/api/v1/stream":
                # GUI push, 本 phase 不实现
                await send_close(writer)
            else:
                await send_close(writer)
        else:
            # 把已读的 body 跟剩余的拼回去 (Content-Length 路径暂时忽略, body 为空 OK)
            await handle_http(reader, writer, method, path, headers, body, registry, db)

    except Exception as e:
        import traceback
        tb = traceback.format_exc().splitlines()
        print(f"[master] client error: {type(e).__name__}: {e}", flush=True)
        # 只打 application 层 frame (跳过 stdlib)
        for line in tb:
            if "pre/src" in line or "pre/scripts" in line:
                print(f"[master]   {line}", flush=True)
        try:
            writer.close()
        except Exception:
            pass


# ---------- 心跳监督 ----------

async def task_summary_loop(registry: Registry, db: MasterDB,
                             interval: float = 60.0):
    """周期对 busy/blocked_user agent 调 LLM 生成 20 字任务总结."""
    import asyncio
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from task_summarizer import summarize_agent_task

    while True:
        try:
            await asyncio.sleep(interval)
            import time as _time
            now = _time.time()
            # 收集待跑的 agent (节流后)
            todo = []
            for agent_id, act in list(registry.activity.items()):
                state = act.get("state")
                if state == "offline":
                    continue
                existing = registry.get_task_summary(agent_id)
                if existing and existing.get("ts"):
                    age = now - existing["ts"]
                    # 节流加倍 (busy 60→180s, idle 300→1800s)
                    if state in ("busy", "blocked_user") and age < 180:
                        continue
                    if state == "idle" and age < 1800:
                        continue
                todo.append((agent_id, act))
            # 排序: busy/blocked_user 优先, 其次没 summary 的, 最后按 ts 旧的优先
            def _prio(item):
                aid, a = item
                state = a.get("state")
                state_rank = {"blocked_user": 0, "busy": 1, "idle": 2}.get(state, 3)
                exists = registry.get_task_summary(aid)
                ts_rank = exists.get("ts", 0) if exists else 0  # 没 ts 的 (= 0) 优先
                return (state_rank, ts_rank)
            todo.sort(key=_prio)
            # 限并发 16 → 6 个 / 轮 (gemini 配额节流)
            todo = todo[:6]
            if not todo:
                continue
            # 并发跑 LLM
            async def _run_one(agent_id, act):
                pane = act.get("pane_summary") or ""
                titles = []
                for m in db.query_messages(agent_id=agent_id, limit=10):
                    if m.get("to_agent") != agent_id:
                        continue
                    if m.get("kind") not in ("command", "task_request", "evaluate_request"):
                        continue
                    payload = m.get("payload", {}) or {}
                    raw = (payload.get("task_title") or payload.get("task")
                           or payload.get("text") or "")
                    if raw:
                        titles.append(raw[:60])
                    if len(titles) >= 3:
                        break
                try:
                    summary = await asyncio.to_thread(
                        summarize_agent_task,
                        agent_id, pane, titles,
                        act.get("recent_actions") or [],
                        act.get("claude_status"),
                        45,
                    )
                except Exception as e:
                    print(f"[master] task_summary {agent_id} failed: {e}", flush=True)
                    summary = None
                if summary:
                    registry.set_task_summary(agent_id, summary)
                    print(f"[master] task_summary {agent_id}: {summary}", flush=True)
            await asyncio.gather(*[_run_one(aid, act) for aid, act in todo],
                                  return_exceptions=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[master] task_summary_loop error: {e}", flush=True)


async def heartbeat_monitor(registry: Registry):
    """周期检查 node 心跳, 超时标 offline"""
    while True:
        await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL)
        now = time.time()
        for node_id, info in list(registry.nodes.items()):
            if now - info.last_seen > NODE_HEARTBEAT_TIMEOUT:
                print(f"[master] node {node_id} heartbeat timeout, removing", flush=True)
                registry.remove_node(node_id)


# ---------- 入口 ----------

async def run_master(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                     db_path: str = DEFAULT_DB, secret: str = "pre"):
    db = MasterDB(db_path)
    registry = Registry(db)

    # Phase C (Finding-A 漂移自愈): master 启动 reload nodes registry from db.nodes 表
    # 不依赖 daemon 重连重注册 (event-driven 复原). master.db nodes 表是 SOT, registry 内存重建.
    try:
        cur = db.conn.execute("SELECT node_id, host, capabilities, last_seen, online FROM nodes")
        _reloaded_count = 0
        for row in cur.fetchall():
            _node_id, _host, _caps_json, _last_seen, _online = row
            try:
                _caps = json.loads(_caps_json) if _caps_json else []
            except (ValueError, TypeError):
                _caps = []
            # 重建 NodeInfo (ws_writer=None, 等 daemon 重连 register_node 时 set)
            # online 标 False 因为 ws 还没连, 真重连后会更新
            _ni = NodeInfo(
                node_id=_node_id, host=_host or _node_id,
                capabilities=_caps, ws_writer=None,
            )
            _ni.online = False  # 待 daemon 重连后 register_node 真 True
            _ni.last_seen = float(_last_seen) if _last_seen else 0.0
            registry.add_node(_ni)
            _reloaded_count += 1
        print(f"[master] Phase C 启动 reload nodes registry from db.nodes: {_reloaded_count} known nodes restored "
              f"(daemon 重连后 online=True)", flush=True)
    except Exception as _e:
        print(f"[master] Phase C reload nodes from db failed (fail-safe): {_e}", flush=True)

    # 注册 virtual agents (user.default 等). 源码层 const, 不动态.
    # 没 ws_writer (不是 real driver), forward_send 走 short-circuit + notify_abstract.
    _virtual_node = NodeInfo(
        node_id="virtual.user", host="virtual",
        capabilities=["virtual"], ws_writer=None,
    )
    _virtual_node.online = True
    registry.add_node(_virtual_node)
    for v_aid in VIRTUAL_AGENTS:
        registry.upsert_agent(AgentInfo(
            agent_id=v_aid,
            node_id="virtual.user",
            driver_type="virtual",
            role="user",
            state="idle",
            capabilities=["text-chat-receive"],
            metadata={"is_virtual": True, "display_name": v_aid.split(".", 1)[-1]},
        ))
    print(f"[master] virtual agents registered: {sorted(VIRTUAL_AGENTS)}", flush=True)

    # 启动时 rotate 旧 mobile_audit (>30 天, M5 + )
    try:
        from master.notify_abstract import rotate_old_audit
        rotate_old_audit(days_keep=30)
    except Exception as e:
        print(f"[master] mobile_audit rotate failed: {e}", flush=True)

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, registry, db, secret),
        host, port,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"[master] listening on {addrs}, db={db_path}", flush=True)
    print(f"[master] auth=enabled  bearer={'<env>' if os.environ.get('PRE_SECRET') else '<default>'}", flush=True)
    print(f"[master] origin_whitelist={sorted(ORIGIN_WHITELIST)}", flush=True)
    print(f"[master] endpoints (require Authorization: Bearer <secret> 除 /healthz):")
    print(f"  GET  http://{host}:{port}/healthz   (公开)")
    print(f"  GET  http://{host}:{port}/api/v1/nodes")
    print(f"  GET  http://{host}:{port}/api/v1/agents")
    print(f"  GET  http://{host}:{port}/api/v1/pending")
    print(f"  GET  http://{host}:{port}/api/v1/messages")
    print(f"  POST http://{host}:{port}/api/v1/agents/<id>/send")
    print(f"  POST http://{host}:{port}/api/v1/agents/<id>/decide")
    print(f"  POST http://{host}:{port}/api/v1/nodes/<id>/rediscover")
    print(f"  POST http://{host}:{port}/api/v1/auth/sse-ticket   (Bearer → SSE ticket)")
    print(f"  GET  http://{host}:{port}/api/v1/agents/<id>/transcript/stream?ticket=<x>  (SSE)")
    print(f"  WS   ws://{host}:{port}/node       (Node 接入, Bearer + secret)")

    asyncio.create_task(heartbeat_monitor(registry))

    # Phase C (Finding-A 漂移主动 detect): master lazy 60s ping known_nodes
    # HC-G10 polling 豁免 4 cond:
    # 1. ms 级 IO (registry.get_node 内存查询, 0 ms)
    # 2. 0 LLM cost (无 LLM 调用)
    # 3. alert-only 不修状态 (仅 finding HIGH, 不动 nodes 表)
    # 4. finding HIGH 升 critical 阈值 (60s 阈值未 recover 才 alert, 不每 60s alert)
    async def _lazy_known_nodes_ping_loop():
        _known_offline_since: dict[str, float] = {}
        _alert_threshold_sec = 300.0  # 5min 离线 → alert critical (跨 60s 阈值未 recover)
        while True:
            try:
                await asyncio.sleep(60.0)
                _now = time.time()
                for _node in list(registry.nodes.values()):
                    _nid = _node.node_id
                    if _nid in ("virtual.user",):
                        continue
                    _last = _node.last_seen or 0.0
                    if _now - _last > 90.0 and not _node.online:
                        if _nid not in _known_offline_since:
                            _known_offline_since[_nid] = _now
                        elif _now - _known_offline_since[_nid] > _alert_threshold_sec:
                            # 5min 仍 offline → finding HIGH (一次性, 防 spam)
                            try:
                                from pathlib import Path as _Path_l
                                _findings = _Path_l(_PRE_LOG_ROOT) / "findings"
                                _findings.mkdir(parents=True, exist_ok=True)
                                from datetime import datetime as _dt_l, timezone as _tz_l
                                _ts_l = _dt_l.now(tz=_tz_l.utc).strftime("%Y%m%dT%H%M%SZ")
                                _stuck = _now - _known_offline_since[_nid]
                                _fp = _findings / f"HIGH-known-node-stuck-offline-{_nid}-{_ts_l}.md"
                                if not _fp.exists():
                                    with open(_fp, "w") as _f:
                                        _f.write(
                                            f"# HIGH: known node `{_nid}` stuck offline\n\n"
                                            f"- ts: {_ts_l}\n- node_id: {_nid}\n"
                                            f"- last_seen: {_last:.0f}\n"
                                            f"- offline_for: {_stuck:.0f}s (≥{_alert_threshold_sec}s threshold)\n"
                                            f"- ADR: + Finding-A 漂移自愈\n\n"
                                            f"<phase_c lazy_ping known_node_offline>\n"
                                        )
                                    try:
                                        os.chmod(str(_fp), 0o600)
                                    except OSError:
                                        pass
                            except OSError:
                                pass
                            # mark as alerted (clear from tracking, 等 recover 后再次 detect)
                            _known_offline_since.pop(_nid, None)
                    elif _node.online:
                        _known_offline_since.pop(_nid, None)  # recover, clear track
            except asyncio.CancelledError:
                break
            except Exception as _e:
                print(f"[master] lazy_known_nodes_ping_loop error: {_e}", flush=True)

    asyncio.create_task(_lazy_known_nodes_ping_loop())

    # usage_prober — 10min 一轮抓 sys_claude/sys_gemini/sys_codex 三家 cli 配额 (0 LLM token)
    # [被 cron daemon 替代 v2 — ]
    # 撤掉理由 (HC-G10/A9 polling 禁止精神): 此 loop 600s 周期, 0 LLM cost (cli 内置命令查 metering API,
    # 不调 completion), 但精神上仍是 polling. 改 cron daemon schedule 触发, 单次脚本 scripts/usage_probe_once.py.
    # 整体注释保留作历史 (最高宪法). cron schedule entry 在 pre_rule/cron/schedules.json 的 pre_usage_probe.
    # async def usage_prober_loop(interval: float = 600.0):
    # sys_path_added = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # if sys_path_added not in sys.path:
    # sys.path.insert(0, sys_path_added)
    # from master.usage_prober import _probe_all_async
    # while True:
    # try:
    # data = await _probe_all_async()
    # # 不让"不完整"的新数据覆盖已有的"完整"旧数据 (sticky 保护) — 已迁到 /api/v1/usage/snapshot endpoint
    # old = registry.usage or {}
    # merged = dict(data)
    # for provider in ("claude", "gemini", "codex"):
    # new_v = data.get(provider) or {}
    # old_v = old.get(provider) or {}
    # new_status = new_v.get("status")
    # old_status = old_v.get("status")
    # incomplete = ("probe_inconclusive", "status_bar_only", "unknown", "error")
    # if new_status in incomplete and old_status not in incomplete and old_status:
    # merged[provider] = old_v
    # print(f"[master] usage sticky: keep old {provider}.status={old_status} (new={new_status})", flush=True)
    # registry.usage = merged
    # print(f"[master] usage probed: claude={merged['claude'].get('status')} gemini={merged['gemini'].get('status')} codex={merged['codex'].get('status')}", flush=True)
    # except Exception as e:
    # print(f"[master] usage_prober error: {e}", flush=True)
    # await asyncio.sleep(interval)
    # asyncio.create_task(usage_prober_loop(interval=600.0))

    # 撤掉 task_summary_loop polling, 改 event-driven (stop_analyzer 每次 stop POST 推一次)
    # 旧 loop 5min × 6 agents = 1728 calls/day max; 新模式仅 stop 时跑 ≈ 用户活跃才跑
    # asyncio.create_task(task_summary_loop(registry, db, interval=300.0)) # [被 替代]

    # master-connect — 主动 connect 远端 node (ssh tunnel + ws client + 重连)
    # 配置在 pre_rule/remote_nodes.json, 没配置 → no-op
    try:
        from master.remote_node_manager import start_remote_node_managers
        await start_remote_node_managers(registry, db, dispatch_node_message)
    except Exception as e:
        print(f"[master] remote_node_manager init failed: {e}", flush=True)

    # : master 内嵌 cron loop (supersedes cron daemon 独立进程)
    # 跨 node schedule 通过 ws RPC exec_cmd 推目标 node, 复用 master-connect ws 通道
    try:
        from master.cron import cron_loop as _master_cron_loop
        asyncio.create_task(_master_cron_loop(registry, db))
    except Exception as e:
        print(f"[master] cron init failed: {e}", flush=True)

    async with server:
        await server.serve_forever()
