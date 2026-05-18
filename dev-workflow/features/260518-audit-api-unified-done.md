# audit API 统一 — 8 类 audit jsonl 暴露给 pre_ui

## 动机

master 端 7 类 audit jsonl 各自独立 schema (历史原因), 唯一 HTTP read endpoint
`/api/v1/notify/audit` 只暴露 `mobile_audit_*.jsonl` 一类. 其余 6 类
(`telemetry / read_pane / agent_data / caller_class / mcp / driver_decision`) 对
pre_ui 不可见; CLI 只有 `scripts/gover_view.py` 看 driver auto_decision.

→ 加 2 个统一 endpoint, 把 8 类 audit 都暴露给 pre_ui, 同时保留 `/notify/audit`
兼容老前端.

## 数据源 (8 kind)

| kind | 路径 (相对 `PRE_LOG_ROOT`) | 文件 glob | 写入方 |
|---|---|---|---|
| `mobile` | `cron/` | `mobile_audit_*.jsonl` | `src/master/notify_abstract.py` |
| `telemetry` | `security/` | `telemetry_audit_*.jsonl` | `master.server._audit_telemetry` |
| `read_pane` | `security/` | `read_pane_audit_*.jsonl` | `master.server._audit_read_pane` |
| `agent_data` | `security/` | `agent_data_audit_*.jsonl` | `master.server._audit_agent_data_read` |
| `caller_class` | `security/` | `caller_class_audit_*.jsonl` | `master.server._audit_caller_class` |
| `mcp` | `mcp_audit/` | `*.jsonl` | `pre_mcp/audit.py` (per-node) |
| `driver_decision` | `codex_driver/` + `gemini_driver/` | `auto_decision_*.jsonl` | `src/drivers/cli_{codex,gemini}_local/driver.py` |
| `gover_review` | `gover_review/` | (待补 — 当前仅 `cron_trigger_*.log` 非 jsonl) | 周期 agent 输出 |

注: `gover_review` kind **暂未列入** KIND 表, 因为目前 `~/cursor/pre_log/gover_review/`
只产 `cron_trigger_*.log` (daemon log, 非 audit jsonl). 待该 agent 写出真实 audit jsonl
后再补.

## 端点设计

### `GET /api/v1/audit/kinds`

返 KIND 元信息 (无 IO, 仅常量). 给前端 tab 用.

**入参**: 无.

**出参**:
```json
{
  "kinds": [
    {
      "kind": "mobile",
      "desc": "user-facing notification dispatch (mobile/webhook/cli)",
      "fields": ["ts","from_agent","to_user","priority","channel","status",
                 "error","payload_size","text_preview","matched_patterns"],
      "filters": {"priority":"exact","from_agent":"substr",
                  "channel":"exact","status":"exact"}
    },
    ...
  ]
}
```

### `GET /api/v1/audit/list`

读 audit jsonl, 应用 filter 与字段白名单, 出口 ISO ts.

**入参** (query string):

| 参数 | 类型 | 默 | 限 |
|---|---|---|---|
| `kind` | string | (必填) | ∈ 7 kind 之一, 否则 400 |
| `since` | unix-epoch float | now - 30d | 强制 ≥ now - 30d |
| `limit` | int | 200 | 1 ≤ x ≤ 500 |
| `<filter_key>` | string | "" | 取决于 kind, 见 `/audit/kinds` |

**出参**:
```json
{
  "kind": "mobile",
  "audit": [ { ...fields按 kind 白名单... }, ... ],
  "total": 123,
  "truncated": false,
  "since": 1779000000.0,
  "limit": 200,
  "filters": {"priority":"high"}
}
```

`truncated: true` 表示命中 limit, 老数据被截.

## 字段白名单 (按 kind)

| kind | 字段 |
|---|---|
| `mobile` | ts, from_agent, to_user, priority, channel, status, error, payload_size, text_preview, matched_patterns |
| `telemetry` | ts, node_id, decision, reason, payload_size, redact_hits, row_id, from_agent_id |
| `read_pane` | ts, caller_token_sha, target_agent_id, target_node, lines_returned, redact_hits, status, raw_disclosed, decision, reason |
| `agent_data` | ts, kind, caller_token_sha, target_agent_id, target_node, bytes_returned, status, decision, reason |
| `caller_class` | ts, caller_class, role, token_label, source_ip, method, path, decision, reason |
| `mcp` | ts, caller_agent_id, tool, args_keys, result_status, latency_ms |
| `driver_decision` | ts, driver, agent_id, tmux_session, tool_name, tool_input_preview, decision, reason, source, action, ok |

**重点排除**:
- `driver_decision.cwd` — 含 `/Users/<user>/cursor/...` home path, 必丢
- `mcp.args` — 写时是 raw dict, 出口转 `args_keys` (只 key list)

**衍生字段**:
- `driver_decision.driver` — 从目录名 `codex_driver` / `gemini_driver` 衍生
- `mcp.args_keys` — `sorted(args.keys())`, 不出 value

**字符串字段二次脱敏**: 所有 str 字段出口前过 `master.redact.redact()` (7 类
SENSITIVE_PATTERNS), fail-safe (异常原样返).

**ts 统一**: 出口都为 ISO 8601 UTC. mcp_audit 写时是 epoch float → endpoint
内转换.

## Filter 矩阵 (按 kind)

| kind | exact filter | substr filter |
|---|---|---|
| `mobile` | priority, channel, status | from_agent |
| `telemetry` | decision | node_id |
| `read_pane` | status, decision | target_agent_id |
| `agent_data` | kind, decision | target_agent_id |
| `caller_class` | role, decision, method | source_ip |
| `mcp` | tool, result_status | caller_agent_id |
| `driver_decision` | driver, tool_name, decision, source, action | agent_id |

KIND.filters 之外的 query 参数静默忽略.

## 鉴权 / 限频 / fail-closed

- **鉴权**: 沿用 `_check_auth` (Bearer token, _required_role_for_path 未指定时仅校
  token 有效, role=None scope=""). 跟现有 `/notify/audit` 一致 — 默认 gui token
  可用; mcp/hook role 也能访问但 `_classify_caller` 限 loopback.
- **限频**: 复用 `_audit_rate_check`, sliding window 60s, 本机使用上调至
  1_000_000/min (`_AUDIT_RATE_LIMIT_PER_MIN`). 429 返 `retry_after: 60`.
- **fail-closed**:
  - `kind` 不在 KIND 表 → 400 `invalid_kind` + valid_kinds 列表
  - `since` < now - 30d → silently clamp 到 30d cutoff
  - `limit` 越界 → clamp 到 [1, 500]
  - jsonl 解析失败 → 跳过该行 (不阻 endpoint)
  - 目录不存在 → 返空 list

## 实现位置

| 文件 | 改动 |
|---|---|
| `src/master/audit_view.py` (新) | KINDS 元表 + `list_kinds()` + `read_entries()` |
| `src/master/server.py` | `/api/v1/audit/kinds` + `/api/v1/audit/list` 两个 elif 分支 (在 `/notify/audit` 之前) |
| `src/master/redact.py` | 复用现有 `redact()` (无改动) |

`/api/v1/notify/audit` 保留为 legacy 兼容入口 (注释新增 NOTE 指向
`/audit/list?kind=mobile`).

## pre_ui 端集成提示

- 加 `audit` tab → `audit.html` + `js/audit.js` + `css/audit.css`
- 拉 `GET /api/v1/audit/kinds` 拿 tab 子项 + filter 维度
- 切 kind 后拉 `GET /api/v1/audit/list?kind=<x>&since=...&limit=200&<filter>=...`
- 按 `truncated` 提示 "命中 limit, 加 filter 或缩 since"
- CSP `connect-src` 已含 `http://127.0.0.1:19500` (`index.html:6`), 无需改

## 测试

- `tests/master/test_audit_view.py` 单元 (待补): KIND 完备性, since 边界, filter
  exact/substr, ts 转换, 字段白名单, 衍生字段
- smoke: master restart → `curl http://127.0.0.1:19500/api/v1/audit/kinds` →
  `curl ".../audit/list?kind=mobile&limit=5"` 对比 `/notify/audit?limit=5`
