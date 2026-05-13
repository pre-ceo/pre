# Token 多来源单点隔离 (~/.pre/env)

**Date**: 2026-05-11
**Status**: done (PR1-5 + 运维 fill 完成)

## 背景

PR1 之前 token 处理散在 ~10 处:
- 6 个 hook/runtime 模块 `os.environ.get("PRE_SECRET", "pre")` 各自读 env, fallback 字符串 "pre" 静默 401
- `scripts/agent_reply.py` / `dispatch_inbox.py` / `read_pane.py` / `decide.py` /
  `upload_file.py` / `download_file.py` 共 6 个 CLI 都接受 `--token` / `--secret` 参数,
  agent 在 shell 里手敲容易把 raw 泄入 transcript
- `src/master/remote_node_manager.py` 跨机调用同 fallback
- `src/node/client.py` + `scripts/start_node.py` `--secret` 同 fallback
- `pre_mcp/master_client.py` 读 `PRE_SECRET`, 但 `~/.pre/env` 里的 PRE_SECRET 是
  mcp-default token, 而 mcp-default `agent_id=None` + `tools.py` 强注 `from_agent` →
  `auth.py:128-133` 直接 403 `mcp_token_missing_agent_id_binding`
- `bus_ctl.sh:273` 用了 `$SECRET` 变量但从未赋值 → 永远 401
- `README.md` 教用户 `curl -H "Authorization: Bearer $PRE_SECRET"` (违背 CLAUDE.md
  "agent 不应自己 curl HTTP" 约定)

用户反馈: token 与派单机制始终有问题; ~/.pre/env 应成为唯一出入口; 强制 agent 用 MCP.

## 方案

按 caller 来源细分多个 token, master 持有全部 hash 并按 (role, source IP) 差异化验证.
~/.pre/env 单点出入口. 不走 minimum-key, 不合并 caller scope.

5 类初始 token (可扩展):

| env key | role | 持有方 | 协议 | 校验 |
|---|---|---|---|---|
| `PRE_NODE_SECRET`     | node     | src/node/ 进程 | WS /node + /files | role=node |
| `PRE_MCP_SECRET`      | mcp      | pre_mcp 子进程 | HTTP loopback | mcp + agent_id binding + loopback |
| `PRE_HOOK_SECRET`     | hook     | hook/runtime/CLI 模块 | HTTP loopback | hook + loopback |
| `PRE_GUI_SECRET`      | gui      | browser (master 颁发) | HTTP(S) | role=gui |
| `PRE_OPERATOR_SECRET` | operator | 人工运维 | HTTP | scope=admin.* |

未来扩展 (cron / audit-reader / remote-node) 走 4 步: auth ROLE 表 + classify_caller
白名单 + `pre_token issue` + token_resolver `_KIND_TO_ENV_KEY` 加 kind.

## 改动清单

### PR1 — 基础设施
- 新建 `src/common/__init__.py`, `src/common/token_resolver.py`
  - `resolve(kind: Literal["node","mcp","hook","gui","operator"]) -> str`
  - `_load_env_file(~/.pre/env)` lazy load, 已有 environ 不覆盖
  - `TokenNotFound` fail-fast (替代旧 "pre" 默认)
- `src/master/auth.py:ROLE_DEFAULT_SCOPES` 加 `gui` + `hook` 两个新 role
- `scripts/start_master.py:_bootstrap_tokens` 列表加 `gui-default` + `hook-default`,
  `initial_tokens.txt` 注释升级 4 → 6
- pre_mcp 不动 (CLAUDE.md 隔离), 行为对齐

### PR2 — Master 来源差异化校验
- `src/master/server.py` 在 `_required_role_for_path` 后插 3 个新函数:
  - `_classify_caller(role, path, source_ip, is_ws_upgrade)` — mcp/hook 必 loopback
  - `_audit_caller_class(...)` — 60/min 限频写 `pre_log/security/caller_class_audit_*.jsonl`
  - `_write_caller_class_finding(...)` — 异常组合写
    `$PRE_LOG_DIR/findings/WARNING-master-caller-class-anomaly-{ts}.md`
- `_check_auth` 签名加 `source_ip`; 在 verify_token 通过后做来源校验, 不通过返 403
- `handle_client` 取 `writer.get_extra_info("peername")[0]` 传给 `_check_auth`
- 错误码识别加 `mcp_role_remote_ip_denied` / `hook_role_remote_ip_denied` → 403

### PR3 — Caller 切到 token_resolver
- 6 hook/runtime 模块顶部加 dual-import (try `from src.common.token_resolver` →
  except `from common.token_resolver`), 调用点换 `_resolve_token("hook")`:
  - `src/cycle_alert.py` (删模块常量 `_MASTER_TOKEN`)
  - `src/runtime/conversation_lifecycle.py` (删模块常量)
  - `src/user_decisions.py:428`
  - `src/ssh_sudo_allowlist.py:352`
  - `src/freerun_intervention_loop.py:183`
  - `src/runtime/process_lifecycle.py:279`
- `src/master/remote_node_manager.py:73,159` → `_resolve_token("node")`
- `scripts/start_node.py:372` `--secret default=None` + 解析后 `_resolve_token("node")`
- `pre_mcp/master_client.py:30` `os.environ.get("PRE_MCP_SECRET")` 优先, fallback
  `PRE_SECRET` (legacy 过渡), 末选 `"pre"` (兼容)

### PR4 — CLI scripts 删 `--token` / `--secret`
- 6 scripts 顶部加 sys.path + `from common.token_resolver import resolve as _resolve_token`
- 删 argparse `--token` / `--secret` 行
- usage 改 `_resolve_token("hook")`
- 涉及: `agent_reply.py` / `dispatch_inbox.py` / `read_pane.py` / `decide.py` /
  `upload_file.py` / `download_file.py`

### PR5 — 文档 + bus_ctl bug
- `bus_ctl.sh`:
  - set -euo 后立即 `source ~/.pre/env` (set -a / set +a wrapper)
  - `NODE_TOKEN` 来源优先 `PRE_NODE_SECRET` (新 schema), fallback legacy
  - line 273-275 修 `$SECRET` undefined bug → `${PRE_HOOK_SECRET:-MISSING_HOOK_TOKEN}`
- `README.md`:
  - mcpServers env 字段 `PRE_SECRET` → `PRE_MCP_SECRET` (英文 + 中文双段)
  - curl 示例加 `source ~/.pre/env` + 改 `PRE_OPERATOR_SECRET` (mode 切换需 admin scope)
- `CLAUDE.md`:
  - 在 "Agent 接入路径" 之后加 "Token 来源 — 唯一出入口 ~/.pre/env" 章节
  - 5 类 token 表 + caller 接入约定 + 加新 kind 4 步 + 4 条禁忌

### 运维 fill (raw 不出 stdout)
- 颁 `hook-default` / `gui-default` 实体 token (PR1 后)
- 同步 `node-default` / `mcp-default` raw 写 `~/.pre/env`
- 颁 `operator-default` 同步写 `PRE_OPERATOR_SECRET` (PR5)
- ~/.pre/env 最终 6 key (5 新 schema + 1 legacy `PRE_SECRET` 过渡期)

## 风险

1. **同用户文件权限拦不住 agent 直接读 `~/.pre/env`** — 技术 enforcement 不行,
   靠 audit + CLAUDE.md 明文约定. PR2 的 `_classify_caller` 加 audit 全部 caller,
   异常调用模式 (mcp role + 非 loopback / hook role + admin path 等) 触发 finding.
2. **~~mcp-default agent_id=None~~ (已修, 同轮 fill-up)** — 颁了 `mcp-bound-pre` token
   绑 `local.cli-claude-code-local.pre`, 替换 `~/.pre/env` 的 `PRE_MCP_SECRET`. **当前会话**
   pre_mcp 子进程仍持旧 token (env 是启动时一次性 load), Claude Code 重启后下次 spawn
   即生效. 别 agent 各自 1 token: 加新 agent 时 `pre_token.py issue --role mcp
   --agent-id <local...your-project>` 颁后写到 ~/.pre/env (或多 agent 单机时用
   `~/.pre/env.d/<agent>` 拆分).
3. **单机多 agent**: PRE_MCP_SECRET 装不下多个 binding token. 后续可能要
   `~/.pre/env.d/<agent>` 分文件. 当前 1 driver 1 进程不阻塞.
4. **GUI token 引导**依赖 pre_ui 实现 `/auth/init` 颁发流程, 本仓只预留接口位
   (`PRE_GUI_SECRET` 在 ~/.pre/env), 跨仓需派单到 pre_ui agent.
5. **`PRE_HOOK_SECRET` 轮换不会 hot reload** — hook 进程是每次新起影响小;
   master/node 长驻需重启.

## 验证

PR1-4 各自 smoke:
- token_resolver: 6 kinds + 未知 kind raise + 缺 env raise + 设 env 后正确返
- master: KNOWN_ROLES 6 个 / `_classify_caller` mcp+hook 非 loopback 拒 / 60/min 限频
- 6 hook 模块 + remote_node_manager + pre_mcp.MasterClient: import OK + token resolve OK
- 6 CLI scripts `--help` 不再显示 `--token` / `--secret` (argparse 删干净)
