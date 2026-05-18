"""audit_view — read-only audit jsonl 统一视图 (给 pre_ui).

设计:
  master 端 7 类 audit jsonl 各有独立 schema (历史原因). 本模块提供:
    - KINDS: 8 kind metadata 表 (dir/glob/字段白名单/filter 维度)
    - list_kinds() : 给前端 tab 用
    - read_entries(kind, since, limit, filters) : 统一读法

  字段出口三道护栏:
    1. 字段白名单 (按 kind 取, 拒绝额外 key)
    2. 字符串字段走 redact() (二次脱敏, fail-safe)
    3. 单条衍生字段处理 (mcp.args → args_keys; driver.cwd 直接丢)

  ts 统一: 出口全为 ISO 8601 UTC string (mcp_audit 写时是 epoch float, 转换).

  限制不在这里实现 (since ≤30d / limit ≤500 / 30/min 限频), 在 server.py
  endpoint 入参时校验. 本模块假设入参已校验过.

HC-PRE-1 stdlib only.
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------- KIND metadata 表 ----------------
# 每个 kind 描述: dir (相对 PRE_LOG_ROOT) / glob / 字段白名单 / 支持的 filter
# fields: 出口暴露给前端的字段 (顺序无意义, 字典)
# filters: 每个 filter 字段 -> 匹配模式 ("exact" | "substr")
# ts_format: jsonl 文件里 ts 字段格式 ("iso" | "epoch")
# multi_dir: True 表示同 kind 数据散在多目录 (driver_decision: codex+gemini)
KINDS: dict[str, dict] = {
    "mobile": {
        "dir": "cron",
        "glob": "mobile_audit_*.jsonl",
        "ts_format": "iso",
        "fields": ["ts", "from_agent", "to_user", "priority", "channel",
                   "status", "error", "payload_size", "text_preview",
                   "matched_patterns"],
        "filters": {"priority": "exact", "from_agent": "substr",
                    "channel": "exact", "status": "exact"},
        "desc": "user-facing notification dispatch (mobile/webhook/cli)",
    },
    "telemetry": {
        "dir": "security",
        "glob": "telemetry_audit_*.jsonl",
        "ts_format": "iso",
        "fields": ["ts", "node_id", "decision", "reason", "payload_size",
                   "redact_hits", "row_id", "from_agent_id"],
        "filters": {"node_id": "substr", "decision": "exact"},
        "desc": "G11 node telemetry burst audit",
    },
    "read_pane": {
        "dir": "security",
        "glob": "read_pane_audit_*.jsonl",
        "ts_format": "iso",
        "fields": ["ts", "caller_token_sha", "target_agent_id", "target_node",
                   "lines_returned", "redact_hits", "status", "raw_disclosed",
                   "decision", "reason"],
        "filters": {"target_agent_id": "substr", "status": "exact",
                    "decision": "exact"},
        "desc": "G9 cross-agent read_pane audit",
    },
    "agent_data": {
        "dir": "security",
        "glob": "agent_data_audit_*.jsonl",
        "ts_format": "iso",
        "fields": ["ts", "kind", "caller_token_sha", "target_agent_id",
                   "target_node", "bytes_returned", "status", "decision",
                   "reason"],
        "filters": {"kind": "exact", "target_agent_id": "substr",
                    "decision": "exact"},
        "desc": "transcript/file endpoint read audit",
    },
    "caller_class": {
        "dir": "security",
        "glob": "caller_class_audit_*.jsonl",
        "ts_format": "iso",
        "fields": ["ts", "caller_class", "role", "token_label", "source_ip",
                   "method", "path", "decision", "reason"],
        "filters": {"role": "exact", "source_ip": "substr",
                    "decision": "exact", "method": "exact"},
        "desc": "(role, path, IP) triple classification audit",
    },
    "mcp": {
        "dir": "mcp_audit",
        "glob": "*.jsonl",
        "ts_format": "epoch",
        # args 字段在出口转 args_keys (只暴露 key list, 不暴 value 内容)
        "fields": ["ts", "caller_agent_id", "tool", "args_keys",
                   "result_status", "latency_ms"],
        "filters": {"caller_agent_id": "substr", "tool": "exact",
                    "result_status": "exact"},
        "desc": "MCP tool call audit (per-node jsonl)",
    },
    "driver_decision": {
        # 多目录: codex + gemini, driver 字段从目录名衍生
        "dirs": ["codex_driver", "gemini_driver"],
        "glob": "auto_decision_*.jsonl",
        "ts_format": "iso",
        # cwd 字段含 home path, 必丢
        "fields": ["ts", "driver", "agent_id", "tmux_session", "tool_name",
                   "tool_input_preview", "decision", "reason", "source",
                   "action", "ok"],
        "filters": {"driver": "exact", "agent_id": "substr",
                    "tool_name": "exact", "decision": "exact",
                    "source": "exact", "action": "exact"},
        "desc": "codex/gemini driver governor decision audit",
    },
}


def list_kinds() -> list[dict]:
    """给前端 tab 用. 返每 kind 的 desc + fields + filters 元信息.

    不含当日条数 (太贵, 前端拉数据自己 count).
    """
    out = []
    for name, meta in KINDS.items():
        out.append({
            "kind": name,
            "desc": meta["desc"],
            "fields": list(meta["fields"]),
            "filters": dict(meta["filters"]),
        })
    return out


def _redact_str(s: str) -> str:
    """复用 master.redact 的字符串脱敏 (fail-safe, 失败原样返)."""
    try:
        from master.redact import redact
        out, _ = redact(s)
        return out
    except Exception:  # noqa: BLE001
        return s


def _to_iso(ts) -> str:
    """ts 任意格式 → ISO 8601 UTC string. 失败返空字符串."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc) \
                           .strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        except (ValueError, OSError, OverflowError):
            return ""
    if isinstance(ts, str):
        return ts  # 假定写时已是 ISO
    return ""


def _ts_to_epoch(ts) -> Optional[float]:
    """ISO / epoch → epoch float (用于 since 比较). 失败返 None."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return float(ts)
        except (ValueError, OSError):
            return None
    if isinstance(ts, str):
        s = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).timestamp()
        except (ValueError, AttributeError):
            return None
    return None


def _filter_entry(entry: dict, filters: dict, filter_spec: dict) -> bool:
    """按 filter_spec 校 entry 是否命中所有 filter. 任何不命中返 False."""
    for fkey, fval in filters.items():
        if not fval:
            continue
        mode = filter_spec.get(fkey)
        if not mode:
            continue  # 未在 spec 内的 filter 忽略 (server 端已剔过, 防御)
        val = str(entry.get(fkey) or "")
        if mode == "exact":
            if val != str(fval):
                return False
        elif mode == "substr":
            if str(fval) not in val:
                return False
    return True


def _project_fields(raw: dict, allowed: list[str],
                     redact_strings: bool) -> dict:
    """按 allowed 白名单提字段. string 走 redact_str (二次脱敏)."""
    out: dict = {}
    for k in allowed:
        v = raw.get(k)
        if isinstance(v, str) and v and redact_strings:
            v = _redact_str(v)
        out[k] = v
    return out


def _list_files(kind: str, log_root: Path) -> list[tuple[Path, str]]:
    """返 (file_path, driver_or_blank). driver_or_blank 给 driver_decision 用."""
    meta = KINDS[kind]
    out: list[tuple[Path, str]] = []
    if meta.get("dirs"):
        for sub in meta["dirs"]:
            d = log_root / sub
            if d.is_dir():
                for f in d.glob(meta["glob"]):
                    # driver 字段从目录名 "{driver}_driver" 抽 prefix
                    driver = sub[:-len("_driver")] if sub.endswith("_driver") else sub
                    out.append((f, driver))
    else:
        d = log_root / meta["dir"]
        if d.is_dir():
            for f in d.glob(meta["glob"]):
                out.append((f, ""))
    # 按 mtime 倒序 (最新文件优先, 命中 limit 早退出)
    out.sort(key=lambda t: t[0].stat().st_mtime if t[0].exists() else 0,
             reverse=True)
    return out


def read_entries(kind: str, since: float, limit: int, filters: dict,
                  log_root: Path) -> tuple[list[dict], bool]:
    """读 audit jsonl, 应用 filter 与字段白名单. 返 (rows, truncated).

    入参假设 server 端已校:
      - kind ∈ KINDS
      - since ≥ now - 30 * 86400
      - 1 ≤ limit ≤ 500
      - filters 内字段已在 KIND.filters 白名单内 (额外 key 静默忽略)

    rows 按 ts 倒序 (最新在前). truncated=True 表示命中 limit, 可能漏老数据.
    """
    if kind not in KINDS:
        return [], False
    meta = KINDS[kind]
    allowed = meta["fields"]
    filter_spec = meta["filters"]

    rows: list[dict] = []
    truncated = False

    for fpath, driver_label in _list_files(kind, log_root):
        try:
            with open(fpath, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # since 过滤
                    e_ep = _ts_to_epoch(raw.get("ts"))
                    if e_ep is None or e_ep < since:
                        continue
                    # 衍生字段处理 (在 filter 之前, 让 filter 能匹配)
                    if kind == "mcp":
                        args = raw.get("args")
                        if isinstance(args, dict):
                            raw = dict(raw)
                            raw["args_keys"] = sorted(args.keys())
                    elif kind == "driver_decision":
                        raw = dict(raw)
                        raw["driver"] = driver_label
                    # filter
                    if not _filter_entry(raw, filters, filter_spec):
                        continue
                    # 投影白名单 + string redact
                    proj = _project_fields(raw, allowed, redact_strings=True)
                    # ts 统一 ISO
                    proj["ts"] = _to_iso(raw.get("ts"))
                    rows.append(proj)
                    if len(rows) >= limit:
                        truncated = True
                        break
        except OSError:
            continue
        if len(rows) >= limit:
            break

    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return rows, truncated
