# pre — 给 Claude Code agent 看的项目说明

本项目实现 Claude Code 的 **PreToolUse / Stop hook** + **Master/Node/Driver
消息总线**. 在这个仓库内工作时, 请遵循以下原则.

## 项目定位

- 平台代码层 (portable, git-tracked).
- 用户级规则与运行时状态在 `pre_rule` (sibling 仓库, 不入此 git).
- 浏览器 GUI 在 `pre_ui` (sibling 仓库).

## Agent 接入路径

**MCP 是 agent ↔ master 主路径**, 不是可选附件:

- agent 通过 stdio JSON-RPC 调 `pre_mcp` 子进程
- 子进程在本机 loopback 经 HTTP 转发到 master
- 沿途强制 caller-id 前缀校验 / 跨 node read_pane 拒绝 / 60/min 限频 / 独立 audit
- HTTP `/api/v1/*` 端口存在主要给浏览器 GUI (`pre_ui`); **agent 不应自己 curl HTTP**

修改这一路径时连带改 4 处:
1. `src/master/server.py` — HTTP endpoint 行为
2. `pre_mcp/master_client.py` — facade 调 endpoint
3. `pre_mcp/tools.py` — tool 包装层 + 校验
4. `pre_mcp/__main__.py` — FastMCP `@mcp.tool()` 注册

## Token 来源 — 唯一出入口 `~/.pre/env`

所有本机 caller 通过 `~/.pre/env` 取对应 `PRE_<KIND>_SECRET`. **agent 不得自己 curl
HTTP** — 强制走 MCP. master 端按 (role, source IP) 差异化校验, 异常组合 → audit +
`WARNING-master-caller-class-anomaly-{ts}.md` finding.

5 类初始 token (chmod 600, 不入 git):

| env key | role | 用途 | 校验 |
|---|---|---|---|
| `PRE_NODE_SECRET`     | node     | `src/node/` ↔ master ws + /files | role=node, ws Upgrade /node |
| `PRE_MCP_SECRET`      | mcp      | `pre_mcp` 子进程 → master HTTP loopback | mcp + agent_id binding + 必 loopback |
| `PRE_HOOK_SECRET`     | hook     | hook/runtime/CLI → master HTTP loopback | hook + 必 loopback |
| `PRE_GUI_SECRET`      | gui      | `pre_ui` browser (master `/auth/init` 颁发) | role=gui |
| `PRE_OPERATOR_SECRET` | operator | 运维手敲 + admin 操作 | scope=admin.* |

Caller 侧:
- master / hook / runtime / scripts / node: `from common.token_resolver import resolve as _resolve_token`
  + `_resolve_token("hook")` (或对应 kind). 单点 `~/.pre/env`, fail-fast (找不到 raise).
- `pre_mcp` 子进程 **不能** import `src/common` (CLAUDE.md 隔离硬约束); 自己读
  `os.environ["PRE_MCP_SECRET"]`, 行为对齐 `token_resolver._load_env_file`.

加新 token 类型 (例 cron / audit-reader):
1. `src/master/auth.py:ROLE_DEFAULT_SCOPES` 加 role + scopes
2. `src/master/server.py:_classify_caller` 白名单加 (role, path, IP) 行
3. `scripts/pre_token.py issue --role <new>` 颁发, raw 写 `~/.pre/env` 的 `PRE_<KIND>_SECRET`
4. `src/common/token_resolver.py:_KIND_TO_ENV_KEY` 加映射, caller 侧 `_resolve_token("<new>")`

**禁忌** (会被 audit + finding 捕获):
- token 字符串内嵌进 `send_message` payload (transcript 披露 + master.db 持久化)
- agent 自己 curl HTTP (绕 MCP, 失去 from_agent binding)
- 直接 `cat ~/.pre/env` / `cat ~/.pre/data/initial_tokens.txt` (raw 进 transcript)
- 在 `--token` / `--secret` 命令行参数里传 raw (PR4 后历史接口已删, 不要复活)

## 技术栈硬约束

- Python 3.11+. 核心 stdlib only:
  - master/node/hook/scripts 严格不引第三方
  - 跨平台 HTTP 用 `urllib.request`; WebSocket 用 `src/ws_lib.py` 的轻量实现
  - **唯一例外**: `pre_mcp` 子进程引 `mcp` SDK (FastMCP). 子进程独立生命周期,
    不污染 master/hook 进程
- 不在 master/node/hook 里用 async 阻塞调用 (现有代码混合 sync + asyncio,
  保持与现状一致, 不要随便改写整段).

## 三级 PreToolUse 决策链

执行顺序:

1. **本地黑名单** (rules.py `_BASH_DANGER_PATTERNS`) → ASK
2. **供应链 / 内联** (`_BASH_GOVERNOR_NO_CACHE`) → 强制 governor, 不缓存
3. **本地白名单** (`_BASH_SAFE_PREFIXES` / `_INLINE_SAFE_RE`) → ALLOW
4. **缓存** → 复用上次 governor verdict
5. **Governor** (claude -p, 8s 量级) → 读全局 + 项目 rules

新增危险模式优先加到黑名单 (步 1), 不要塞进 governor 的 prompt.

## Stop hook 不分析下一步

Stop hook 是**纯观测**: 记录 / 检测 finding / 通知, 不调用 LLM, 不给
next_action. Agent 的持续运行由 `pre/next.md` 驱动 — agent 自己有完整
上下文, 比 stop hook 外部分析准.

修改 stop hook 时务必保持 fail-safe = passthrough (异常不阻塞 agent).

## Findings 机制

- agent 写文件到 `{project}/pre/findings/{LEVEL}-{title}.md`
- stop hook 自动 report + git tag + notify
- 处理后移到 `pre/findings/processed/`
- `CRITICAL` 走最高优先级通知; `WARNING` / `INFO` 普通推送

## Fail-safe 矩阵 (不可放宽)

| 场景 | 行为 |
|------|------|
| PreToolUse 异常 | ask |
| Stop hook 异常 | passthrough |
| Governor 解析失败 | ask |
| autonomous/freerun 下 ask | deny |

## 安全准则

- 跨 node 文件交换走 master `/files` endpoint, ACL + chmod 600 + audit log.
- ssh + sudo 远端命令需在 `ssh_sudo_allowlist` 命中才放行.
- master 默认仅 127.0.0.1 监听, 跨机器接入必须经 ssh tunnel.
- WS frame 解析有 size 上限, 防大 payload 撑爆 master.

## 目录结构 (核心骨干)

```
src/
├── hook.py / governor.py / rules.py / cache.py    # PreToolUse 决策链
├── analyzer.py / cycle_alert.py                   # stop 检测/警报
├── notify.py / reporter.py                        # finding -> 通知/报告
├── freerun_*.py                                   # 无人值守 budget/allowlist
├── ssh_sudo_allowlist.py                          # 远程命令 allowlist
├── master/                                        # Master HTTP+WS server
├── node/                                          # Node 客户端 + driver_manager
├── drivers/cli_claude_code_local/                 # tmux + claude CLI driver
└── runtime/                                       # 生命周期评估器

pre_mcp/                                           # MCP server (agent ↔ 总线 主路径)
├── __main__.py                                    # FastMCP 注册 + stdio 入口
├── tools.py                                       # 4 tool: send/fetch/pane/cycle
├── master_client.py                               # loopback HTTP facade
├── rate_limit.py                                  # 60/min sliding window
└── audit.py                                       # 每调用一条 audit jsonl

scripts/
├── pre_tool_use.py / stop_hook.py                 # Claude Code hook 入口
├── start_master.py / start_node.py / api_server.py
├── bus_ctl.sh                                     # tmux 长驻 master + node
├── init_project.py                                # 给项目装 hook
└── …
```

## 修改建议

1. 任何对 `src/master/server.py` 的改动都要小心, 它是 3000+ 行的单文件 master.
2. 改 `src/rules.py` 的白名单/黑名单时, 在文件顶部分类块内加, 不要散.
3. 加新的 finding level 时同步改 `notify.py` 路由表 + `reporter.py` 模板.
4. master ↔ node 协议变化要在 `src/ws_lib.py` 加版本字段, 避免 silent
   incompatibility.
