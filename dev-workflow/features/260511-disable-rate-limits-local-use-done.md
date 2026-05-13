# 本机使用放开 API 限频

**Date**: 2026-05-11
**Status**: done

## 背景

用户反复撞 429:
- GUI 卡 `sending` (memory: master 共享 read_pane 限频桶 transcript/sessions/files 共用 30/60s)
- SSE 流连不上, 错误 `{error: "too_many_active_tickets", detail: "...:8/8"}`

pre master/MCP 现行限频是为多用户/远端部署写的, 但当前是单机单用户使用,
没有 DoS 风险, 各档阈值过低反而干扰正常 GUI 轮询和 agent 并发.

用户决策: 取消所有 API 限额, 本机适用不怕.

## 方案

7 处限频常量统一上调到 `1_000_000` (足够大, 实际不会撞), 保留 sliding-window /
audit / stats 代码结构以便未来重新启用或观测.

**未动**: `conversation_lifecycle` 的 `in_cooldown` (compact/evaluate 后业务冷却,
返 429 但属业务延迟不属 API 限频).

## 改动清单

### src/master/server.py

| 常量 | 原值 | 新值 | 影响范围 |
|---|---|---|---|
| `RATE_LIMITS[critical]` | per_agent 9999 / global 100/min | 1_000_000 / 1_000_000 | chat send `/api/v1/agents/<id>/send` |
| `RATE_LIMITS[high]` | per_agent 30 / global 200/min | 1_000_000 / 1_000_000 | 同上 |
| `RATE_LIMITS[normal]` | per_agent 10 / global 300/min | 1_000_000 / 1_000_000 | 同上 |
| `_READ_PANE_RATE_LIMIT` | 30/60s/caller | 1_000_000 | read_pane + transcript/sessions/files 共享桶 |
| `_SSE_MAX_CONN_PER_TOKEN` | 4 并发 | 1_000_000 | SSE `transcript/stream` 长连接 |
| `FILE_RATE_LIMITS[upload]` | 100/h/agent | 1_000_000 | `/files` upload |
| `FILE_RATE_LIMITS[download]` | 500/h/agent | 1_000_000 | `/files` download |
| `_AUDIT_RATE_LIMIT_PER_MIN` | 30/min/Bearer | 1_000_000 | `/notify/audit` + last-success endpoint 复用 |

### src/master/sse_ticket.py

| 常量 | 原值 | 新值 | 影响 |
|---|---|---|---|
| `MAX_PER_CALLER` | 8 张活跃 ticket | 1_000_000 | SSE one-time-use ticket 并发上限 |

错误格式从 `too_many_active_tickets:N/8` 变成 `too_many_active_tickets:N/1000000` —
若用户后续看到 `:N/8` 即说明老 master 进程未替换.

### pre_mcp/rate_limit.py

| 常量 | 原值 | 新值 | 影响 |
|---|---|---|---|
| `SlidingWindowRateLimiter.max_per_window` (默认参数) | 60/min/agent | 1_000_000 | MCP tool 调用 (agent→master loopback) |

注: `pre_mcp` 是 Claude Code agent 自己 spawn 的 stdio 子进程, **不随 master 重启**
而刷新; 需要 agent (Claude Code) 重启才能 pick up 新值. 不影响 master→browser SSE 路径.

## 验证

1. **语法**:
   ```bash
   python3 -m ast src/master/server.py src/master/sse_ticket.py pre_mcp/rate_limit.py
   # 三文件 OK
   ```

2. **bus 重启**:
   ```bash
   bash scripts/bus_ctl.sh restart
   # master/node/ui/cron 四 session 全部重启 ok
   ```

3. **运行时验证 (master 进程内)**:
   ```bash
   .venv/bin/python3 -c "
   import sys; sys.path.insert(0, 'src')
   from master import sse_ticket
   print(sse_ticket.MAX_PER_CALLER)
   "
   # → 1000000
   ```

4. **浏览器侧**: 硬刷 (Cmd+Shift+R) 清掉 cached error response, SSE 重连后不再撞 8/8.

## 陷阱

- **不删代码结构**: `_RATE_WINDOWS` / sliding-window prune 逻辑保留, audit jsonl
  写盘逻辑保留. 未来想重新限频改阈值即可.
- **pre_mcp 子进程独立生命周期**: 改 `pre_mcp/rate_limit.py` 不影响当前已 spawn
  的子进程. agent 重启后才生效. 但 MCP 限频跟 SSE ticket / GUI 卡 sending 不在
  同一路径, 不影响主要痛点.
- **`in_cooldown` 不是 API 限频**: `conversation_lifecycle` (compact/evaluate
  操作冷却) 也返 429, 但语义是业务延迟. 未动, 如果未来想取消单独议.
- **rules.py 白名单已含 `bash scripts/bus_ctl.sh` 前缀**: agent 跑 restart 不会
  撞 PreToolUse 黑名单.

## Rollback

把 7 个常量改回原值, restart bus 即可. 改动表里第 2 列是原值.

## 相关 memory

- `project_master_rate_limit_shared.md` — GUI 卡 sending 常见根因记录, 本次直接
  根治 (放开共享桶); memory 仍保留用于解释历史.
