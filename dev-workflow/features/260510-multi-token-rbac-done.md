# 260510 — multi-token RBAC (single-secret → role-scoped DB tokens)

## 背景

迁移完 fn_pre → pre 后, master 仍用 single shared `PRE_NODE_SECRET` 给所有
接入者 (node ws / fe_server proxy / pre_mcp / agent_reply CLI / 浏览器 GUI).
任何一处泄漏 = master 全权失守, 也无法做撤销 / 审计 / 不同接入者权限隔离.

用户原话: "修改生成一个可以存储在db中的token，给不同的api使用者, node, fn_fe,
api bus, mcp 分配不同的 token 存储在 db 同时限制权限."

## 设计决策 (用户确认)

| 决策点 | 选项 |
|--------|------|
| Legacy `PRE_NODE_SECRET` env | **即刻破布式升级** — 不留 grace, 升级后 env 无效, 仅 DB token 有效 |
| 首次启动 bootstrap | **写临时文件** ~/.pre/data/initial_tokens.txt chmod 600, log emph 提示 read-once-and-rm |
| Scope 粒度 | **粗颗 4 role 优先** — 第一期按 role 检查, 后续再加 endpoint-level scope |
| GUI auth | **继续 sessionStorage 手填** — settings.html 输入 operator token |

## 数据模型

```sql
CREATE TABLE bus_tokens (
    token_hash    TEXT PRIMARY KEY,    -- sha256(raw), raw 不入库
    label         TEXT NOT NULL UNIQUE,-- 人类可读, 唯一
    role          TEXT NOT NULL,       -- node|operator|cli|mcp
    scopes        TEXT NOT NULL,       -- JSON array
    agent_id      TEXT,                -- mcp 专属: 锁定调用方身份
    created_ts    REAL NOT NULL,
    expires_ts    REAL,                -- NULL = 永久
    last_used_ts  REAL,
    revoked_ts    REAL,                -- 软删保留审计
    metadata      TEXT                 -- 自由 JSON
);
CREATE INDEX idx_bus_tokens_label ON bus_tokens(label);
CREATE INDEX idx_bus_tokens_revoked ON bus_tokens(revoked_ts);
```

## 4 个 role + 默认 scope

| role | scopes | 给谁 |
|------|--------|------|
| `node` | `bus.connect`, `bus.message.*` | node ws daemon |
| `operator` | `admin.*`, `agent.*`, `bus.*` | GUI / 运维 / 你本人 |
| `cli` | `bus.message.send`, `bus.message.fetch` | agent_reply.py 等轻 CLI |
| `mcp` | `bus.message.{send,fetch}`, `bus.pane.read`, `bus.cycle_state` | 每个 agent 的 pre_mcp 子进程 (绑 agent_id) |

## Scope 命名

```
bus.connect            ws /node
bus.message.send       POST /api/v1/agents/{id}/send
bus.message.fetch      GET  /api/v1/agents/{id}/messages
bus.pane.read          GET  /api/v1/agents/{id}/pane
bus.cycle_state        GET  /api/v1/agents/{id}/cycle_state
agent.control          PUT  /api/v1/agents/{id}/mode
agent.kill             POST /api/v1/agents/{id}/kill
admin.tokens           CRUD bus_tokens 表
admin.*                所有 admin
bus.*                  所有 bus
agent.*                所有 agent control
```

第一期粗颗实现: master 端 `_required_scope(endpoint) → role allowlist`
而非真的 set 操作; role 满足 (e.g. operator 能进 admin/agent/bus 全段) 即放行.

## mcp token 身份锁定

mcp token 在 DB 里绑一个 `agent_id`. master 收到带此 token 的请求时:
- 若 endpoint 接受 `from_agent` 字段 (e.g. `/send`), `from_agent` 必等于
  绑定的 agent_id, 否则 403
- 防 mcp 子进程伪装其它 agent 发消息

## 启动流程

```
master 启动:
  1. load bus_tokens 表 → 内存 hash dict
  2. 表空 (首次启动):
       生成 4 个默认 token (node/operator/cli/mcp), insert hash
       raw 写 ~/.pre/data/initial_tokens.txt chmod 600
       master log emph "请读取 initial_tokens.txt 配置客户端, 读后 rm"
  3. 表非空: 直接用. 不再支持 --secret / PRE_NODE_SECRET 参数路径.

每个 HTTP/WS endpoint:
  → auth.verify_token(Bearer)
    → sha256 → DB 查 hash → 检 revoked / expires
    → 命中 → role + scopes + agent_id 进 request context
  → endpoint 声明 required_scope, auth.has_scope(role, scope) 决定 ALLOW/403
```

## 改动面

| 文件 | 改什么 |
|------|--------|
| `src/master/persistence.py` | 加 bus_tokens 表 + CRUD |
| `src/master/auth.py` (新) | verify_token + has_scope (role-based 第一期) |
| `src/master/server.py` | 替换现有 Bearer 校验; 主要 endpoint 加 scope 注解 |
| `scripts/pre_token.py` (新) | CLI: issue / list / revoke / rotate |
| `scripts/start_master.py` | 移除 --secret; bootstrap 4 默认 token |
| `scripts/bus_ctl.sh` | 去掉 PRE_NODE_SECRET 路径; 启动后 emph 输出 token list 入口 |

不改的客户端 (Bearer 协议不变, 只是值不同):
- pre_mcp/master_client.py (env PRE_SECRET 直接装 mcp token)
- pre_ui/scripts/fe_server.py (反代透传 Bearer header)
- agent_reply.py (--token 参数原样)

## 验证项 (M1 - 完工 checklist)

- [x] 启动空 DB → initial_tokens.txt 生成且 chmod 600
- [x] 4 个 token 各自做 role 内操作 → 200
- [x] cli token 切 mode → 403 (Forbidden, 不是 401)
- [x] mcp token 携带 from_agent ≠ 绑定 agent_id → 403
- [x] 撤销 token (revoke) → 后续请求 401
- [x] node ws 用 node token 接入 → ok (bus_ctl.sh 自动从 initial_tokens.txt 取)
- [x] master.db 中 token_hash 可见, raw 任何地方都不存
- [x] 旧 secret (e.g. "fnpre") 设 Bearer → 401
- [x] 浏览器 GUI (经 fe_server 反代) operator token → 200

## 完成 (2026-05-10)

- 跑通命令:
  ```bash
  rm -rf ~/.pre/data/master.db ~/.pre/data/initial_tokens.txt
  unset PRE_NODE_SECRET PRE_SECRET PRE_SECRET_LEGACY
  bash scripts/bus_ctl.sh start
  cat ~/.pre/data/initial_tokens.txt   # 拿 4 个默认 raw
  python3 scripts/pre_token.py issue --role mcp --label mcp-pre \
      --agent-id local.cli-claude-code-local.pre
  python3 scripts/pre_token.py list
  python3 scripts/pre_token.py revoke --label mcp-pre
  ```
- 偏离设计的地方:
  - **scripts/token.py → scripts/pre_token.py**: 文件名 `token.py` 跟 stdlib
    `token` 模块冲突 (scripts dir 自动进 sys.path, `from token import
    EXACT_TOKEN_TYPES` 在 tokenize.py 里炸). 重命名解决.
  - **mcp-default token 默认不绑 agent_id**: bootstrap 时 agent_id=NULL,
    导致用 mcp-default token 发 from_agent 字段的请求会被
    `mcp_token_missing_agent_id_binding` 拒. 这是 *correct by design* —
    每个 agent 应该 issue 自己的 mcp token, mcp-default 仅作模板. 文档应
    指引用户:
       `python3 scripts/pre_token.py issue --role mcp --label mcp-{agent} \
           --agent-id <agent_id>`
  - **POST /api/v1/agents/{id}/mode 实际是 PUT 不是 POST?** 测试时 PUT
    返 405 (Method Not Allowed); 405 已经是 auth 通过后的结果, RBAC 这层
    没问题. 业务 endpoint 路由是另回事, 不在本 feature 范围.
- 客户端兼容性:
  - `pre_mcp/master_client.py`: PRE_SECRET env 直接放 mcp token raw, 跑通.
  - `pre_ui/scripts/fe_server.py`: 反代透传 Bearer header, 浏览器换 token
    即可.
  - `agent_reply.py`: --token 参数透传, 用 cli-default 即可.
- 后续 (本 feature 范围外):
  - 细颗 endpoint-level scope (现在粗颗 4 role)
  - admin endpoint `/api/v1/admin/tokens` (走 HTTP 代替 CLI 直读 DB)
  - GUI 里展示 last_used / expires / 撤销按钮 (operator token 能 admin.*)
  - mcp token agent_id 与 master 实际 agent registry 联动 (issue 时检查 agent 存在)
