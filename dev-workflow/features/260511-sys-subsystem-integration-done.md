# sys_* monitor 子系统集成 (cron-driven probe + 零配置自启动)

**Date**: 2026-05-11
**Status**: done

## 背景

`sys_claude` / `sys_gemini` / `sys_codex` 是 LLM cli 配额监控 tmux session, 跑相应 cli
+ 周期发 `/usage` / `/model` / `/status` 抓 quota (0 LLM token, cli 内置命令).

fn_pre 时代这套是分散外部依赖: user 手敲 `tmux new-session ...` 起 sys_*, `spawn_agent.sh`
辅助, master 内嵌 `usage_prober_loop` 周期 probe. fn_pre 后期把内嵌 loop 注释掉了
(master event loop 不该 send-keys 干扰 cli pane), 改用外部 cron 触发 `usage_probe_once.py`.

用户对 pre 的指引:

> pre 需要集成一个自己的 cron 实现一些低频的循环访问操作, 这不是 loop, 而是日报
> 类型的, usage 属于其中 15min 访问 (因为没有 token 消耗) 的一个中频操作. 使用
> 自建 cron 启动 probe, 然后 probe 负责保活保 sys 可用.

后续追加:

> 那其实 sys_claude sys_gemini 都可以自己启动了 (零配置)

最终设计: sys_* 完全归 pre 平台管 — cron 周期触发 probe, probe 自己 spawn 缺失的
session, 自己处理 trust dialog. **user 零干预**.

## 架构 (4 个组件)

```
master 内嵌 cron (src/master/cron.py, 30s tick)
       ↓ 读 pre_rule/cron/schedules.json
       ↓ pre_usage_probe entry (interval=900s)
       ↓ subprocess.Popen detached
scripts/usage_probe_once.py
       ↓ _probe_all_async 抓 sys_* pane (zero LLM token)
       ↓ session 缺失 / cli 异常 → _respawn (force_kill on streak)
       ↓ spawn 后自动 confirm trust dialog
       ↓ POST /api/v1/usage/snapshot + severity event
master/server.py /api/v1/usage* + usage_snapshot_v2 表
       ↓ 默认 stale filter, ?include_stale=true 显式
T-Deck / GUI / agent 消费
```

## 改动清单

### 新增文件

| 路径 | 行数 | 说明 |
|---|---|---|
| `src/master/usage_prober.py` | 569 | parser (`_parse_claude` / `_parse_gemini` / `_parse_codex`) + `_PROBE_SPECS` + `_probe_all_async` + persistence helpers |
| `scripts/usage_probe_once.py` | 480 | cron entry, probe 一次 + 健康检查 + spawn 保活 + trust auto-confirm |
| `scripts/spawn.rc` | 21 | pre 内置 minimal fallback (只 `source ~/rule.sh`) |
| `scripts/cleanup_stale_usage_snapshots.py` | (上 backport 已加) | 配 stale filter 清 DB |
| `$PRE_RULE_ROOT/spawn.rc` | 40 | **user 配置层**, 默认含 JP egress 校验 example (跟 fn_pre 等价行为) |
| `$PRE_RULE_ROOT/cron/schedules.json` | 18 | cron entry, `pre_usage_probe` interval=900s target=local |

### `_PROBE_SPECS` schema

```python
_PROBE_SPECS = {
    "claude": {
        "session": "sys_claude",
        "command": "/usage",
        "parser_kwargs": {"cleanup_key": "Escape"},
        "default_enabled": True,
        "spawn_cli": "claude",  # 零配置自启动用
    },
    "claude_foxbn": {...},          # apikey 模式, 默认 disabled
    "gemini": {
        "session": "sys_gemini",
        "command": "/model",        # dialog 含 Flash/Pro/Lite 各 %used + Resets
        "parser_kwargs": {"cleanup_key": "Escape", "extra_keywords": ("Resets:", "Flash", "Pro"), "timeout_total": 10.0},
        "default_enabled": True,
        "spawn_cli": "gemini",
    },
    "codex": {
        "session": "sys_codex",
        "command": "/status",
        "parser_kwargs": {},
        "default_enabled": True,
        "spawn_cli": "codex",
    },
}
```

Provider enable/disable 配置: `$PRE_RULE_ROOT/usage_probe.json` (缺文件用
`default_enabled`).

### spawn.rc 双层架构

借用 pre_rule 是配置目录的概念 — JP egress 校验 / 代理 env 等 user 部署特定逻辑
归 user 层, pre 平台代码保持 minimal:

```
解析优先级 (probe `_resolve_spawn_rc`):
  1. $PRE_SPAWN_RC env
  2. $PRE_RULE_ROOT/spawn.rc           ← user 配置 (含 JP egress 校验 example)
  3. /root/workspace/pre_rule/spawn.rc ← 远端 fallback
  4. <pre>/scripts/spawn.rc            ← pre 内置 minimal (只 source ~/rule.sh)
  5. /root/workspace/pre/scripts/spawn.rc
```

user 编辑 `pre_rule/spawn.rc` 修改 / 关闭 IP 校验, 无需碰 pre git tracked code.

### 零配置自启动设计决策

1. **cwd 用 `~/.pre/sys_workdir/`** — probe 自动 mkdir 0700. 不污染 user 仓库
   (`$PRE_ROOT/` 会 load CLAUDE.md + hook, 干扰 sys cli; `$HOME` 也不行,
   claude 会扫 HOME 文件).
2. **spawn_cli hardcoded** in `_PROBE_SPECS` — sys_* 是平台 monitor 不是 user
   project, 不依赖 `$PRE_PARENT/sys_*/pre/agent_config.json`. user 想 customize 改
   spawn.rc 里 alias 命令.
3. **不加 `--dangerously-skip-permissions`** — 会引入二次 "Bypass Permissions"
   弹窗 (默认指针在 `No, exit` 反向危险). sys_claude 只跑 `/usage` slash, 本来
   就不触发 file edit ask UI, 普通启动够用.
4. **trust dialog auto-confirm** — claude code v2 第一次进新 cwd 弹
   "Quick safety check / Yes, I trust this folder". probe spawn 后 sleep 7s
   验 has-session, 再 capture pane, 含 "trust this folder" 字串就 send-keys
   Enter (默认指针在 1. Yes). gemini / codex 没此弹窗, capture 命中也无害.

### 健康检查 + respawn 策略

| 状态 | 行为 |
|---|---|
| `ok` / `limit_reached` / `near_limit` | 清 fail_streak |
| `skipped (... not found)` (session 缺失) | 立即 respawn (除非 cool-down) |
| `error` / `probe_inconclusive` / `unknown` / `status_bar_only` 连续 ≥3 次 | force_kill + respawn (session 在但 cli 异常) |
| 其他 `skipped` (config 关闭) | 不动 |

- cool-down 5min 防 spawn fail 时风暴 (e.g. rc 校验持续失败)
- streak counter 持久化 `~/.pre/data/probe_health/{node_id}_{provider}.json`
- audit log `$PRE_LOG_DIR/probe_health_respawn.log` chmod 600

### 半成品 snapshot quarantine

even `status='ok'`, 缺 cur/week/reset 任一 = 格式没出来, 视为半成品:
- POST snapshot 时 quarantine 不发 (master sticky 保留旧好数据)
- `_do_respawn_pass` 内降级为 `probe_inconclusive`, 走 streak 触发 respawn

`_REQUIRED_FIELDS`:
- claude: session_percent_used, week_percent_used, session_reset, week_reset
- codex: percent_left_5h, percent_left_week, reset_5h, reset_week
- gemini: models dict 任一带 reset_at

### cron schedule (pre_rule/cron/schedules.json)

```json
{
  "version": 1,
  "schedules": [
    {
      "id": "pre_usage_probe",
      "type": "interval",
      "every_seconds": 900,
      "enabled": true,
      "target_node": "local",
      "cmd": ["uv", "run", "python", "scripts/usage_probe_once.py"],
      "cwd": "<absolute path to pre repo, e.g. $PRE_ROOT>",
      "env": {"PRE_MASTER_URL": "http://127.0.0.1:19500"}
    }
  ]
}
```

master cron loop 30s tick 自动 hot reload. token 走
`from common.token_resolver import resolve` `_resolve_token("hook")` 从
`~/.pre/env::PRE_HOOK_SECRET` 取.

## 实战验证

```
$ uv run python scripts/usage_probe_once.py
[probe-health] claude → respawn (reason=session_not_found streak=0)
[probe-spawn] claude: auto-confirmed trust dialog
[probe-health] claude respawn result: ok
   ↓ 下一轮 probe (15min 后 cron 自动触发, 或手动跑)
[usage-probe] snapshot resp: {'v2_results':
  [{'provider': 'claude', 'account': '<user>@example.com', 'ok': True, 'action': 'inserted'},
   {'provider': 'gemini', 'account': '<user>@example.com', 'ok': True, 'action': 'updated'}]}
[usage-probe] claude: severity unknown -> ok → post event

$ curl '/api/v1/usage' (via PRE_HOOK_SECRET)
claude | <user>@example.com | status=ok | used_pct=9.0
gemini | <user>@example.com | status=ok | used_pct=8.0
```

## 已知行为 (by design)

### sys_codex spawn 失败 (user 机器没装 codex cli)

每 15min cron 触发 → `tmux new-session ... bash -ic "source spawn.rc && exec codex"`
→ rc 跑通但 `exec codex` "command not found" → session died 7s 后被 has-session
检测出 → audit log "fail(session died after spawn (rc check failed?))" → 5min
cool-down → 下次再试.

按 user 指示: 接受这种状态, 不特殊处理. 装 codex cli (`npm i -g @openai/codex`
等) 后自然 work.

### sys_claude_foxbn 默认 disabled

apikey 模式 cli pane 数据不全, 后续走 `POST /api/v1/usage/external` 统一 API 输入.
user 想启用改 `pre_rule/usage_probe.json::providers.claude_foxbn.enabled=true`.

### 跨 node probe

`--node-id` flag 让远端 cron 跑时传 node 名. cwd 路径用 `~/.pre/sys_workdir/`,
跨机器 portable (跟本机项目路径布局解耦, 不需要 `/root/workspace/` 这种远端
fallback).

## 跟之前 PR 的关系

- backport `260511-fn-pre-features-backport-done.md` 含 `usage stale filter`
  (server.py 端). 本次 sys-subsystem 是上游 producer 完整集成 (probe + cron +
  spawn), 配合 backport 的 stale filter (consumer side) 构成闭环.
- multi-token RBAC `260510-multi-token-rbac-done.md` + token-isolation
  `260511-token-multi-source-isolation-done.md` — usage_probe_once.py 走
  `token_resolver("hook")` 从 `~/.pre/env::PRE_HOOK_SECRET` 取, 符合现有体系.

## 文件树

```
pre/
├── src/master/
│   ├── usage_prober.py          # parser + spec + probe_all_async (569 行)
│   └── cron.py                  # 已有, 不动
├── scripts/
│   ├── usage_probe_once.py      # cron entry + 保活 (480 行)
│   ├── spawn.rc                 # 内置 minimal fallback
│   └── cleanup_stale_usage_snapshots.py
└── dev-workflow/features/
    └── 260511-sys-subsystem-integration-done.md  ← this file

$PRE_RULE_ROOT/
├── spawn.rc                     # user 配置 (含 JP egress example)
└── cron/
    ├── schedules.json           # pre_usage_probe entry
    └── state.json               # cron runtime state

~/.pre/
├── sys_workdir/                 # sys_* tmux cwd (probe 自动 mkdir 0700)
└── data/probe_health/
    └── {node_id}_{provider}.json  # streak counter
```
