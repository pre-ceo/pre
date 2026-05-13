# pre — Claude Code Agent Governance & Multi-Agent Bus

[中文版](#中文) · [English](#english) · [License: MIT](LICENSE)

PreToolUse / Stop hook governance + a Master/Node/Driver message bus for
orchestrating multiple Claude Code agents — local and remote — under a single
control plane.

```
Claude Code Agent
  │
  ├─ Tool call ─→ PreToolUse Hook  (0–8s)
  │    1. Blacklist        → ASK/DENY  (rm -rf, sudo, force-push, …)
  │    2. Supply-chain     → Governor, no cache (npm, node -e, ssh, …)
  │    3. Whitelist        → ALLOW    (git, ls, find, read in cwd, …)
  │    4. Cache            → reuse last governor verdict
  │    5. Governor (LLM)   → reads global rules + project rules
  │
  ├─ Stop ─→ Stop Hook  (passive observer)
  │    • record stop log
  │    • process pre/findings/{LEVEL}-{title}.md
  │    • freerun/autonomous: block + tell agent to read pre/next.md
  │    • supervised: passthrough
  │
  └─ Bus access
       Agent ──MCP stdio──▶ pre_mcp subprocess ──loopback HTTP──▶ Master
                            (4 tools: send_message / fetch_inbox /
                                       read_pane / cycle_state)

       Master (HTTP + WS) ◀──▶ Node(s) ◀──▶ Driver(s) ◀──▶ Agent process(es)
       Browser GUI (pre_ui) ◀── HTTP only ──▶ Master
```

**Why MCP, not raw HTTP for agents?** Agents talk to the bus through the
`pre_mcp` server (stdio JSON-RPC) — it enforces caller-identity prefix
checks (M7-2), cross-node read-pane denial (M7-4), 60/min sliding-window
rate limits, and structured audit logs before forwarding to master.
HTTP is exposed for the browser GUI (which can't speak MCP) and for the
master ↔ node bus protocol — agents themselves should never curl
`/api/v1/*`.

This repository is the **code** layer (portable, git-tracked). The full
deployment has three pieces:

| Name | Role | Source | Personal data |
|---|---|---|---|
| **pre** (this repo) | code + control plane | `git clone https://github.com/pre-ceo/pre.git` | no |
| `pre_rule/` | user-editable rules + local runtime state (global PreToolUse rules, notify config, agent caches, logs) | created by `install.sh` from `pre/templates/pre_rule/` (no separate upstream) | yes (local; never pushed upstream by `pre`) |
| [`pre_ui`](https://github.com/pre-ceo/pre_ui) | browser GUI for driving the master from a CEO-style operator console | cloned by `install.sh` from `https://github.com/pre-ceo/pre_ui.git` (override with `--pre-ui-url=URL`) | no |

You can `git init` inside `pre_rule/` and push to a private remote for
multi-host sync — `pre` does not push for you.

---

<a id="english"></a>
## English

### What this gives you

1. **PreToolUse governance for Claude Code.** Every tool call goes through
   a layered decision pipeline: instant local rules (whitelist / blacklist /
   inline-safe regex), a verdict cache, and finally an LLM-driven *governor*
   that reads global + project rules. Dangerous patterns (`rm -rf`, force
   push, `curl | sh`, supply-chain installs, inline `node -e` / `python -c`,
   ssh remote commands) are surfaced as **ASK** in supervised mode and
   automatically **DENY** in autonomous / freerun mode.
2. **Stop hook as observer, not interrupter.** Stop is zero-latency — it
   just records, detects findings written by the agent, and (in unattended
   modes) nudges the agent to read `pre/next.md`. Findings of severity
   `INFO` / `WARNING` / `CRITICAL` route through pluggable notification
   channels.
3. **Master / Node / Driver bus.** A single master (HTTP REST + WebSocket)
   talks to one or more nodes (each potentially on a separate machine via an
   SSH tunnel). Each node hosts pluggable *drivers* that own actual agent
   processes — the included driver wraps `claude` CLI sessions running in
   tmux.
4. **Freerun safety net.** Budget caps (token / cost / runtime / LLM-calls
   per day), event-driven loops only (no polling sleeps), an allowlist that
   can absorb repeated patterns instead of dying on every ASK, and a kill
   switch that converts every freerun back to half-supervised.

### Directory layout

```
pre/
├── src/
│   ├── hook.py / governor.py / rules.py / cache.py     ← PreToolUse pipeline
│   ├── analyzer.py / cycle_alert.py / transcript_parser.py
│   │                                                    ← Stop-side helpers
│   ├── notify.py / reporter.py                          ← findings -> notify
│   ├── freerun_*.py                                     ← unattended-mode safety
│   ├── ssh_sudo_allowlist.py                            ← remote-cmd allowlist
│   ├── master/                                          ← Master HTTP+WS server
│   ├── node/                                            ← Node client + driver mgr
│   ├── drivers/                                         ← Agent process drivers
│   └── runtime/                                         ← lifecycle / evaluator
├── pre_mcp/                                             ← MCP server (agent ↔ bus)
│   ├── __main__.py                                      ←   stdio JSON-RPC entry
│   ├── tools.py                                         ←   4 tools: send/fetch/pane/cycle
│   ├── master_client.py                                 ←   loopback HTTP facade
│   ├── rate_limit.py                                    ←   60/min sliding window
│   └── audit.py                                         ←   per-call audit jsonl
├── scripts/
│   ├── pre_tool_use.py / stop_hook.py                   ← Claude Code hook entry
│   ├── start_master.py / start_node.py / api_server.py
│   ├── bus_ctl.sh                                       ← tmux process supervisor
│   ├── init_project.py                                  ← bootstrap project pre/
│   ├── spawn_agent.sh                                   ← launch a tmux+claude agent
│   └── …                                                ← inbox/decide/cron etc.
└── docs/
```

`src/master/usage_prober.py`, all tests, and CDP / Twitter business probes
from the upstream tree are intentionally excluded — this repository is the
core skeleton.

### Quick start

> Python 3.11+. The hooks, master, node, and scripts are stdlib-only;
> the optional `pre_mcp` subpackage needs the `mcp` SDK.

```bash
export PRE_DIR=$HOME/your-path/pre
git clone https://github.com/pre-ceo/pre.git "$PRE_DIR"
cd "$PRE_DIR"

# 1. one-shot bootstrap (idempotent, re-runnable on upgrade):
#    - creates pre_rule/ next to pre/ from templates/pre_rule/
#    - clones pre_ui/ next to pre/ from https://github.com/pre-ceo/pre_ui.git
#    - registers mcpServers.pre in ~/.claude.json
#    - writes ~/.pre/env (paths) + installs pre / pre-tool-use /
#      pre-stop-hook shims into ~/.local/bin (offers to add it to PATH)
bash scripts/install.sh
uv add mcp                              # optional: install MCP SDK

# 2. start the master + a local node (tmux-supervised)
pre bus start

# 3. wire your project to the hooks
pre init /path/to/your-project --mode supervised
# This creates ./pre/{agent_config.json,rules.md,next.md,findings/}
# and merges PreToolUse + Stop hooks into ./.claude/settings.json
```

After that, every tool call from Claude Code in that project flows through
the PreToolUse pipeline; the agent can also call MCP tools
`mcp__pre__send_message`, `mcp__pre__fetch_inbox`, `mcp__pre__read_pane`,
`mcp__pre__cycle_state` to interact with the bus.

Edit `pre/rules.md` to add project-specific allow / ask / deny rules;
edit `pre/next.md` to give the agent a self-driving plan when it would
otherwise stop.

#### What `install.sh` does to `pre_rule/`

The `pre_rule/` directory is split into a *system* layer that `install.sh`
manages, and a *global* layer that you own:

| Layer | Files | install.sh behavior |
|---|---|---|
| **system** (do not edit) | `system.md`, `system_analyze.md`, `.gitignore`, `README.md`, `LICENSE` | Overwritten on every install. If you edited one, it is backed up to `<file>.bak.<ts>` and a diff summary is printed. |
| **global** (yours) | `global.md`, `global_analyze.md`, `spawn.rc`, `config.json` | Created on first install; subsequent installs **never overwrite** them. |

The governor prompt is assembled as `system.md` (contract + safety floor) →
`global.md` (operator policy) → `<project>/pre/rules.md` (per-project).
Same layering for the analyzer with `*_analyze.md`.

To sync rule changes across machines, `git init` inside `pre_rule/` and push
to a private remote yourself — `pre` does not push for you.

### Agent loop modes

| Mode        | PreToolUse danger | Stop behavior                        | Needs `pre/next.md` |
|-------------|-------------------|--------------------------------------|---------------------|
| supervised  | ASK user          | passthrough                          | no                  |
| autonomous  | **DENY**          | block + agent reads `pre/next.md`    | **yes**             |
| freerun     | **DENY**          | block + agent reads `pre/next.md`    | **yes**             |

Switch via the control API (used by the GUI; agents themselves should call
the equivalent MCP tool rather than curl HTTP):

```bash
# operator command-line, NOT from inside an agent.
# Source ~/.pre/env first; mode-change requires admin scope (operator role).
source ~/.pre/env
curl -X PUT http://127.0.0.1:19500/api/v1/agents/{id}/mode \
     -H "Authorization: Bearer $PRE_OPERATOR_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"mode": "freerun"}'
```

### Findings — agent-driven notifications

When an agent decides something is worth a human's attention it writes:

```bash
echo "details…" > pre/findings/CRITICAL-something-broke.md
echo "details…" > pre/findings/WARNING-perf-regression.md
echo "details…" > pre/findings/INFO-coverage-up.md
```

Stop hook automatically:

1. Renders a report under `pre/reports/{ts}-{level}-{title}.md`.
2. Tags the git tree (`finding/{level}/{ts}`) when the project is a git repo.
3. Routes through the configured notification channel(s) — webhook /
   tmux-bell / log — with severity-aware policy.
4. Moves the finding to `pre/findings/processed/`.

### Fail-safe matrix

| Situation                | Behavior      |
|--------------------------|---------------|
| PreToolUse exception     | **ask**       |
| Stop hook exception      | **passthrough** (never blocks the agent) |
| Governor parse failure   | **ask**       |
| `ask` under autonomous / freerun | **deny** (no human to confirm) |

### License

MIT — see [LICENSE](LICENSE).

---

<a id="中文"></a>
## 中文

### 项目定位

为 Claude Code agent 提供**调用前置治理 + 多 agent 总线**:

- **PreToolUse hook**: 黑/白名单 + 缓存 + LLM governor 三级决策, 把
  危险操作 (`rm -rf`, force push, `curl | sh`, 供应链安装, 内联代码执行,
  ssh 远程命令) 在 supervised 下抬升为 ASK, 在 autonomous/freerun 下
  自动 DENY.
- **Stop hook** 仅观测: 记录日志、处理 agent 写入的 `pre/findings/`,
  无人值守模式下 block 并指引 agent 读 `pre/next.md`.
- **Master/Node/Driver 总线**: master 单点 (内部 HTTP REST + WebSocket,
  仅本机/loopback), node 跨机器接入 (SSH tunnel + WebSocket), driver 控制
  实际 agent 进程 (内置 driver 包装 tmux 中的 `claude` CLI 会话).
- **MCP 是 agent 接入总线的主路径**: agent 通过 stdio JSON-RPC 调
  `pre_mcp` 子进程 (4 工具: `send_message` / `fetch_inbox` / `read_pane` /
  `cycle_state`), 子进程在本机经 loopback HTTP 转发到 master, 沿途做
  caller-id 前缀校验 + 跨 node read_pane 拒绝 + 60/min sliding window 限频
  + 每次调用独立 audit jsonl. **agent 不应自己 curl `/api/v1/*`**, HTTP
  端口主要给浏览器 GUI (`pre_ui`).
- **Freerun 防御**: budget cap (token / cost / runtime / 每日 LLM 调用),
  禁止 polling, allowlist 自动吸收重复 ASK 模式, kill switch 一键把所有
  freerun 转回半 supervised.

### 三件套分工

| 名称 | 角色 | 来源 | 含个人数据 |
|------|------|------|----------------|
| **pre** (本仓库) | 代码与控制平面 | `git clone https://github.com/pre-ceo/pre.git` | 否 |
| `pre_rule/` | 用户级规则与运行时状态 | `install.sh` 从 `templates/pre_rule/` 创建 | 是 (本机, 不入上游 git) |
| [pre_ui](https://github.com/pre-ceo/pre_ui) | 浏览器 GUI | `install.sh` 自动 clone (URL: `https://github.com/pre-ceo/pre_ui.git`) | 否 |

### 快速开始

> 需 Python 3.11+. hook / master / node / scripts 仅标准库;
> 可选的 `pre_mcp` 子包需要 `mcp` SDK.

```bash
export PRE_DIR=$HOME/your-path/pre
git clone https://github.com/pre-ceo/pre.git "$PRE_DIR"
cd "$PRE_DIR"

# 1. 一站式 bootstrap (幂等, 升级时重跑即可):
#    - 从 templates/pre_rule/ 创建 sibling pre_rule/
#    - clone sibling pre_ui/ (URL: https://github.com/pre-ceo/pre_ui.git)
#    - 在 ~/.claude.json 注册 mcpServers.pre
#    - 写 ~/.pre/env (路径) + 把 pre / pre-tool-use / pre-stop-hook shim
#      装到 ~/.local/bin (提示是否加进 PATH)
bash scripts/install.sh
uv add mcp                              # 可选: 装 MCP SDK

# 2. 起 master + 本机 node (tmux 长驻)
pre bus start

# 3. 给项目装上 hook
pre init /path/to/your-project --mode supervised
# 自动创建 ./pre/{agent_config.json, rules.md, next.md, findings/}
# 并把 PreToolUse + Stop hook 合并进 ./.claude/settings.json
```

完成后该项目里 Claude Code 的所有工具调用都走 PreToolUse 决策链;
agent 自己也可以调 MCP 工具 `mcp__pre__send_message` / `fetch_inbox` /
`read_pane` / `cycle_state` 跟总线交互.

#### install.sh 对 pre_rule/ 的分层处理

`pre_rule/` 拆 *system* 与 *global* 两层, install.sh 区别对待:

| 层 | 文件 | install.sh 行为 |
|---|---|---|
| **system** (不要改) | `system.md`, `system_analyze.md`, `.gitignore`, `README.md`, `LICENSE` | 每次 install 强制覆盖. 内容跟模板不同时, 旧文件备份成 `<file>.bak.<ts>` 并打印 diff 摘要 |
| **global** (你的) | `global.md`, `global_analyze.md`, `spawn.rc`, `config.json` | 首次创建; 之后再跑 install.sh **不会覆盖** |

Governor prompt 拼接顺序: `system.md` (合约 + 安全底线) → `global.md`
(operator 策略) → `<project>/pre/rules.md` (项目级). Analyzer 用
`*_analyze.md` 对同理.

多机 sync 规则: `cd $PRE_RULE_ROOT && git init && git remote add origin <private>`, 自己推自己 pull. `pre` 不会替你推.

### 目录结构

```
pre/
├── src/
│   ├── hook.py / governor.py / rules.py / cache.py     ← PreToolUse 决策链
│   ├── analyzer.py / cycle_alert.py / transcript_parser.py
│   │                                                    ← stop-side 辅助
│   ├── notify.py / reporter.py                          ← finding -> 通知
│   ├── freerun_*.py                                     ← 无人值守安全网
│   ├── ssh_sudo_allowlist.py                            ← 远程命令 allowlist
│   ├── master/                                          ← Master HTTP+WS 服务
│   ├── node/                                            ← Node 客户端与 driver
│   ├── drivers/                                         ← agent 进程驱动
│   └── runtime/                                         ← 生命周期 / 评估器
├── pre_mcp/                                             ← MCP server (agent ↔ 总线)
│   ├── __main__.py                                      ←   stdio JSON-RPC 入口
│   ├── tools.py                                         ←   4 工具
│   ├── master_client.py                                 ←   loopback HTTP facade
│   ├── rate_limit.py                                    ←   60/min 滑窗限频
│   └── audit.py                                         ←   每次调用 audit jsonl
├── scripts/                                             ← 入口脚本
└── docs/
```

### Agent 循环模式

| 模式 | PreToolUse 危险操作 | Stop 行为 | 需 `pre/next.md` |
|------|---------------------|-----------|------------------|
| supervised | ASK 用户 | passthrough | 否 |
| autonomous | **DENY** | block + 读 `pre/next.md` | **是** |
| freerun    | **DENY** | block + 读 `pre/next.md` | **是** |

切换:

```bash
# 运维手敲 (mode 切换需 admin scope, operator role)
source ~/.pre/env
curl -X PUT http://127.0.0.1:19500/api/v1/agents/{id}/mode \
     -H "Authorization: Bearer $PRE_OPERATOR_SECRET" \
     -d '{"mode": "freerun"}'
```

### Findings 机制

agent 把重要发现写入 `pre/findings/{LEVEL}-{title}.md`, stop hook 自动:

1. 生成报告到 `pre/reports/`,
2. 项目是 git repo 时打 tag `finding/{level}/{ts}`,
3. 经配置的 notify channel 推送 (按 severity 路由),
4. 移到 `pre/findings/processed/`.

### Fail-safe

| 场景 | 行为 |
|------|------|
| PreToolUse 异常 | **ask** |
| Stop hook 异常 | **passthrough** (不阻塞 agent) |
| Governor 解析失败 | **ask** |
| autonomous / freerun 下 ask | **deny** (无人确认) |

### License

[MIT](LICENSE)
