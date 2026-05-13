# 260513 — codex + gemini driver 拉齐 + 统一 pre-init [DONE]

## 实际产出

### 新增代码
- `src/drivers/cli_gemini_local/__init__.py` — exports DRIVER
- `src/drivers/cli_gemini_local/driver.py` — 全套 BaseDriver 接口
  (discover/send/get_state/detect_pending/decide/detect_activity/init_agent),
  driver 内嵌 evaluator + pane scrape + auto allow/deny + jsonl audit
- `src/drivers/cli_gemini_local/pending_parser.py` — Gemini approval 解析
  (`Allow execution of [<tool>]?` + `● 1. Allow once` marker), Shell/Edit/Write
  tool 解析 + generic fail-closed
- `dev-workflow/features/260513-codex-gemini-driver-unified-init-done.md` (本文)

### 修改代码
- `src/drivers/base.py` — 提 `InitResult` dataclass + `init_agent` 契约 +
  `cli_name` 类属性到基类 (此前只在 claude driver)
- `src/drivers/cli_claude_code_local/driver.py` — 移除本地 InitResult 改 import
  自 base, 加 `cli_name="claude"`
- `src/drivers/cli_codex_local/driver.py` — pointer-based discover (砍
  cursor_root 兜底) + `_failed_spec` + send/decide/detect_* 加 active 检查 +
  `init_agent()` (4 步, 砍 hook 步) + `_extract_codex_llm_route` (tomllib +
  has_oauth via ~/.codex/auth.json)
- `src/master/server.py` — 修 `/api/v1/agents` 502 RemoteDisconnected
  (`payload.get("task")` 派单消息里是 dict, `raw[:60]` 抛 `unhashable type:
  'slice'`; 加 isinstance check + dict fall back goal/subject/text/description);
  client error log 改打全 traceback (之前只打 pre/src frame, 漏 stdlib 根因行)
- `src/hook.py` — 仅 docstring 增补"codex/gemini 不调本 hook"说明
- `src/prehook_evaluator.py` + `src/analyzer.py` — dual import (relative +
  absolute fallback), 修 driver `from prehook_evaluator import ...` 顶层
  import 时抛 `attempted relative import with no known parent package`
  (codex driver 也借此修, 此前从未真正测过 evaluator 路径)
- `scripts/pre_init.py` — driver registry `{claude, codex, gemini}` +
  `--driver` flag (默认 claude, 向后兼容)
- `scripts/pre` — `_cmd_list` 跑全 3 driver + 加 `--driver` filter, 输出
  `[ok|failed] agent_id (cli)` 三列
- `scripts/spawn_agent.sh` — 读 `agent_config.json` 的 `cli` 字段反推
  driver type_name + `start_command` 默认; `.claude/settings.json` hook
  仅 claude 写 (codex/gemini 跳过, 走 driver pane scrape)
- `scripts/bus_ctl.sh` — `NODE_CAPABILITIES` 默认含三 driver
  (`cli-claude-code-local,cli-codex-local,cli-gemini-local`)
- `scripts/start_node.py` — `--capabilities` help text + `stuck_detector`
  循环纳入 `cli-gemini-local`

## 技术总结

### 主要设计决策

1. **gemini driver 跟 codex 同模式, 不走 hook 路径** — gemini 原生有
   `BeforeTool`/`AfterAgent` hook (跟 claude PreToolUse/Stop 同 schema, 事件名
   不同), 但 hook decision schema 只支持 `allow/deny` 二态, 没 `ask`. 实测
   `pre-tool-use` shim 输出 claude `hookSpecificOutput` schema, gemini 不识别
   走 fallback "Allow". 真正 ask 必须 driver pane scrape (用户原话 "让 gemini
   跟 codex 的 driver 保持一致"). gemini 在 default approval-mode 弹原生
   approval UI (`Allow execution of [<tool>]?` + `● 1. Allow once / 2. ... /
   3. No, suggest changes`), driver detect_pending pane scrape → 内嵌
   evaluator → 自动注 "1" allow / "Esc" reject / 上报 ask.

2. **InitResult + init_agent 契约提到 BaseDriver** — 此前 claude driver
   独有 InitResult dataclass + init_agent 5 步. 提到 base 让 codex/gemini
   实现对应 4 步 (砍 hook 写入). `pre_init.py` 用 driver registry 按
   `--driver` 路由.

3. **`spawn_agent.sh` 按 `cli` 字段反推 driver type_name** — 此前
   `agent_id="$NODE_ID.cli-claude-code-local.$PROJECT"` 写死 claude type.
   现读 `agent_config.json` 的 `cli`, 映射:
   `claude→cli-claude-code-local`, `codex→cli-codex-local`,
   `gemini→cli-gemini-local`. `start_command` 默认按 cli 选.

4. **codex LLM route 包含 OAuth 检测** — codex 默认推荐 ChatGPT 账号
   OAuth (auth.json 持 token), `config.toml` 可不含 explicit model.
   `_extract_codex_llm_route` 加 `has_oauth: bool` 检测
   `~/.codex/auth.json` 存在, 区分 OAuth vs API key 模式.

5. **prehook_evaluator dual import** — `from .config import` (relative)
   在 hook.py 经 `src` package 调用时 work; 但 driver `from
   prehook_evaluator import` 顶层 import 时, prehook_evaluator 内部的
   relative imports 抛 `attempted relative import with no known parent
   package` (codex driver 一直 broken). 加 try/except fallback 到
   absolute imports. analyzer.py 同样修.

6. **master /api/v1/agents 502 silent close 根因** — `handle_http` 抽
   task_title 时, `payload.get("task")` 在 fn_pre 派单消息里是 dict
   (`{goal, files_to_fix, ...}`), `dict[:60]` 抛 `unhashable type:
   'slice'`. master 接 TCP 后直接 close, client 看 `Empty reply from
   server`. log 之前只打 pre/src frame, stdlib 那行 (`raw[:60]`) 真错点
   被过滤掉, 这个 bug 之前一直找不到. 现改用 isinstance 检查 + dict 抽
   goal/subject/text/description, 同时 log 打全 traceback.

### Gemini 跟 Claude TUI 差异 (实测)

| 维度 | Claude TUI | Gemini TUI v0.42 |
|---|---|---|
| busy 标志 | Simmering… / Pondering… / esc to interrupt | Thinking… / Generating / Loading / spinner ⠋⠙⠹ |
| idle 锚点 | `? for shortcuts` / `new task?` | `Type your message` / `? for shortcuts` / `Shift+Tab to accept edits` |
| approval 前缀 | "Do you want to proceed?" + `❯ 1. Yes` | "Allow execution of [<tool>]?" + `● 1. Allow once` |
| approve/reject key | 1 / Escape | 1 / Escape (相同) |
| 工具调用 history marker | `⏺` / `⎿` | `✓ Shell <cmd>` / `o Shell <cmd>` |
| status line | `✻ Cooked for 3m 4s` | `Flibbertigibbeting… 4m · ↓ N tokens` (不解析) |
| Response 前缀 | (无显式) | `✦ <text>` |
| hook 接口 | PreToolUse / Stop | BeforeTool / AfterAgent (本 driver **不用**) |

### 实战 e2e 验证

`test_gemini` agent 发 `rm /tmp/nonexistent_test_file_xyz123` prompt:
1. gemini 弹 `Allow execution of [rm]?` approval UI
2. driver detect_pending capture-pane
3. parser 解出 `Bash: rm /tmp/nonexistent_test_file_xyz123`
4. evaluator → governor (LLM) 决策 `allow`,
   reason "Specific file removal in /tmp is not a broad destructive operation"
5. driver `send_key("1")` → gemini 跑 rm
6. audit jsonl chmod 600 记录决策 (decision/source/reason/action/ok)

### Master `/api/v1/agents` 验证

修 502 后 5/5 curl 都返 HTTP 200, 13 agents 列表正常返:
- 10 claude (含 stale)
- 1 codex (test_codex, status=ok)
- 1 gemini (test_gemini, status=ok)

## 验证

1. ✅ syntax check: 全部 .py + .sh py_compile / bash -n 都过
2. ✅ driver_manager.load 加载三 driver (claude/codex/gemini)
3. ✅ `pre-init --driver codex/gemini /tmp/xxx` 写出正确 config + pointer + (claude) hook
4. ✅ `pre list` 三 driver 全 yield, 7 ok agents (5 claude + 1 codex + 1 gemini)
5. ✅ codex/gemini agent 上 bus (master.db: status=ok)
6. ✅ gemini driver e2e: prompt → 弹 approval → driver auto-decide → send_key 注入 → rm 跑 → audit log 写
7. ✅ master /api/v1/agents 502 修复, 5/5 curl HTTP 200
8. ✅ opensource audit (5 工具): 0 CRITICAL, 11 HIGH (全 github-org false positive,
   即 `github.com/pre-ceo/` 项目自己仓库 URL), 0 实质 HIGH 满足放行

## 遗留 / 后续

1. **远端 ssh spawn (`spawn_agent.sh:288+`) 还是 claude-only** —
   `G6 precheck claude --version` + 写 `.claude/settings.json` + `exec claude`
   都 hardcode. 远端 codex/gemini 单独排.
2. **codex/gemini approval pane fixture** — `_CODEX_ACTION_RE` 和
   `_extract_recent_gemini_actions` 抓 history 工具调用行的 regex 还
   是占位. 主路径 (detect_pending) 已实测准, 但 `recent_actions`
   字段对历史调用解析需 fixture 实测覆盖.
3. **gemini hook BeforeTool 实测笔记** —
   设置 hook 时必须显式 `"matcher": "*"`, 省略 matcher gemini 0.42 不 fire
   (跟 docs 说"empty = match all"不一致). 留作 future ref;
   本 driver 不走 hook 路径 (跟 codex 一致), 此 note 仅供后续若改回 hook
   方案参考.
4. **`pre_repair.py` 还 hardcode `cli="claude"`** — 极少用, 不阻塞.

<260513-codex-gemini-driver-unified-init>
