# Unit test skeleton — 锁定 pre 核心决策行为

## 动机

pre 在本机长驻 (master + node + hook + pre_mcp), 任何对 `rules.py` / `cache.py` /
`token_resolver.py` / `freerun_*.py` / `ssh_sudo_allowlist.py` / `pre_mcp/rate_limit.py`
的改动都直接影响:

- agent 跑 Bash 时的 ALLOW / ASK / GOVERNOR 决策
- token resolve 的 fail-fast 行为
- freerun mode 下的 budget cap + 命令白名单
- ssh+sudo 远程命令的黑/白名单

历史上只有 2 个 ad-hoc 脚本 (`scripts/test_stuck_detector_provenance.py` /
`scripts/test_cli_codex_local_driver.py`), 不走 pytest, 不在 CI, 不能锁定
回归. 任何一次"加个 regex"或"小调一下 prefix"都可能静默改变实际行为.

## 方案: Layer 1 pure unit (pytest)

按之前讨论的三层方案 (Layer 1 unit / Layer 2 boundary mock / Layer 3 e2e smoke),
本次只补 Layer 1 — 最快, ROI 最高, 覆盖最稳定的 pure function / state machine.

### 引入约束

pre 硬约束: master/node/hook/scripts **stdlib-only**, 第三方只允许在 `pre_mcp` 子进程.

引 pytest 作 **dev-only** 依赖, 不进 runtime:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
]
```

跑法:

```bash
uv run --with pytest pytest          # 默认 tests/
uv run --with pytest pytest -v       # 列每个 case
uv run --with pytest pytest tests/test_rules.py -k inline_safe
```

### 目录结构

```
tests/
├── __init__.py
├── conftest.py                       # sys.path 注入 + 隔离 HOME / pre_rule / pre_log
├── test_rules.py                     # ~60 case: 三级决策链
├── test_cache.py                     # 12 case: verdict 缓存
├── test_token_resolver.py            #  9 case: ~/.pre/env loader
├── test_rate_limit.py                #  7 case: sliding window
├── test_freerun_budget.py            # 11 case: budget cap
├── test_freerun_allowlist.py         # 18 case: T1-T4 tier + kill switch
└── test_ssh_sudo_allowlist.py        # 16 case: ssh wrap + 黑白名单 + audit
```

合计 **150 passed** (~0.36s on M-series Mac).

## 隔离设计 — 不破坏本机生产环境

测试**不**碰真实 `~/.pre/env` / `~/cursor/<rule-repo>` / `~/cursor/<log-repo>` /
`master.db` / tmux sessions / 真实 audit jsonl.

`tests/conftest.py` 的 `_isolate_pre_env` 是 autouse fixture, 每个 test 自动
跑前注入:

| monkeypatch | 保护对象 |
|---|---|
| `HOME = tmp/home` | `~/.pre/env`, `~/.claude/*`, 所有 `Path.home()` 引用 |
| `PRE_RULE_ROOT = tmp/pre_rule` | freerun budgets / allowlist, hook log, cron state, agents/ |
| `PRE_LOG_DIR = tmp/pre_log` | audit jsonl, findings, hook 决策日志 |
| `PRE_AGENT_HOME = tmp/agents` | agent project 工作目录, finding 派单根 |

每个 test 拿独立 `tmp_path`, 测试间不串扰.

**没有任何 test** 启动 master / node / tmux / 调真实 HTTP — 全部纯函数 +
tmp file IO + monkeypatch.

### 验证: 严格 pytest-only window 跑前后 diff

```bash
find ~/.pre ~/cursor/<rule-repo> ~/cursor/<log-repo> -type f \
  | xargs -I{} stat -f "%m %z %N" {} | sort > /tmp/before.txt \
&& uv run --with pytest pytest -q \
&& find ~/.pre ~/cursor/<rule-repo> ~/cursor/<log-repo> -type f \
  | xargs -I{} stat -f "%m %z %N" {} | sort > /tmp/after.txt \
&& diff /tmp/before.txt /tmp/after.txt
```

结果: **diff exit=0** — 150 个 test 跑完, 一个字节都没动真实文件.

### 已知唯一 side effect (无害)

`src/common/paths.py` 顶部有 module-level eager-load `~/.pre/env` 进 `os.environ`.
pytest 进程 import 时会读真实 `~/.pre/env` 一次, 把 token 临时塞到 **pytest 进程自己**
的 environ. 但:

- 只读, 不回写
- pytest 进程退出后 environ 自动消失
- 不污染 shell, 不影响 tmux 里跑的 master / node

## 覆盖详情

### `src/rules.py` (PreToolUse 三级决策链)

锁定以下行为, 任何改动会被立刻发现:

- Read/Grep/Glob: cwd 内 / `~/` dotfile → ALLOW, 越界 → GOVERNOR
- Write/Edit: cwd 内 / `~/.claude/` → ALLOW, 越界 → GOVERNOR
- Bash 黑名单 (`rm -rf` 各变体 / `git push --force` / `DROP TABLE` / `mkfs` /
   `curl | sh` / `chmod 777` / `nc -l`): ASK
- Bash 白名单 (`git status` / `ls` / `cat` / `tmux capture-pane`): ALLOW
- Inline safe (`bash -c "echo"` / `curl http://127.0.0.1` /
   `curl http://localhost:19500/api/v1/*`): ALLOW
- GOVERNOR_NO_CACHE (`npm install` / `pip install` / `node -e` 非 console.log /
   `python -c` 非 print / `&&sudo` / `ssh user@host cmd`)
- Sensitive override (`.ssh/`, `id_rsa`, `.aws/credentials`, `/etc/shadow`,
   `.config/gh/hosts.yml`): GOVERNOR
- Exfil vector (`| curl/wget/nc/ssh/scp/rsync` / `> /home/...`): GOVERNOR
- `> /tmp/` / `> /dev/null` 不触发 exfil
- Unknown tool (WebSearch / Agent) → ALLOW

#### 测试发现的 src 边界 (锁定**当前行为**, 不修)

测试遇到几个 regex 不命中实际命令的边界, 这些不是本次任务范围, 但记录避坑:

- `_BASH_DANGER_PATTERNS` 的 `\bdd\s+if=\b` 在 `dd if=/dev/zero of=...` 上不命中
  (`if=/` 之间无 word boundary). 该 case 实际落到 GOVERNOR 而非 ASK.
- `\bnc\s+-[a-z]*l\b` 要求 `l` 必须在选项末尾 word boundary, `nc -lvnp 4444`
  不命中 (`-lvnp` 末尾不是 l). 推荐用 `nc -l 4444` 测试.
- `node -e 'console.log(1)'` 命中 `_INLINE_SAFE_RE` (console.log 模式), 在
  GOVERNOR_NO_CACHE 之前 ALLOW. 这是有意行为 — 单纯 print/log inline 不必每次
  走 governor.
- `find . | ssh user@host 'cmd'` 命中 `_BASH_GOVERNOR_NO_CACHE` 的
  `\bssh\b.*\s+['\"]` (远程命令), 返 GOVERNOR_NO_CACHE 而非 GOVERNOR.

后续如果想统一 / 修这些 regex, 改完测试断言要同步.

### `src/cache.py` (verdict 缓存)

- `cache_key()` 确定性 + 同输入同 16-char sha256[:16]
- Bash 只取 `command`, 忽略 `description` 等动态字段
- Read/Write/Edit 取 `file_path`, Grep/Glob 取 `pattern` + `path`
- 未知工具用完整 input 的 `sort_keys=True` JSON hash (顺序无关)
- `get_cached()` TTL 过期返 None, 损坏 JSON 不抛
- `set_cached()` 创建 missing dir
- 超过 500 条触发 LRU-ish 按 ts 旧→新清理

### `src/common/token_resolver.py` (`~/.pre/env` loader)

- 5 类 kind (`node` / `mcp` / `hook` / `gui` / `operator`) → `PRE_*_SECRET` 映射
- 缺 env key 抛 `TokenNotFound` (msg 含 `PRE_*_SECRET` 名)
- Unknown kind 抛 `TokenNotFound`
- 引号脱壳 (`"xxx"` / `'xxx'`)
- 已存在 `os.environ` key 不被覆盖 (shell export 优先)
- 注释行 / 空行 / 缺 `=` 不报错
- env file 不存在但 environ 有 → 仍 resolve

### `pre_mcp/rate_limit.py` (sliding window)

- 空 caller_agent_id → reject
- 限内通过 + 计数累积, 触顶 reason 含 `caller_id` + `n/window`
- 不同 caller 窗口独立
- 时间推到窗口外 → 旧记录回收, 重新允许
- `get_limiter()` 单例幂等

### `src/freerun_budget.py` (task budget cap)

- 配置缺 → `_DEFAULT_BUDGET`
- task-specific override 合并 default
- `record_usage` 累加 tokens/cost/runtime/llm_calls
- `check_budget` 三态 ok / warn (≥80%) / exceeded (≥100%) 各维度
- 跨天 reset `llm_calls_today`, 保留累计 tokens/cost/runtime
- `write_budget_finding` 写 HIGH-budget-exceeded-*.md, 已存在不重写

### `src/freerun_allowlist.py` (freerun mode 命令白名单 + tier)

- T1 allow_prefixes 命中 → ALLOW
- T2 deny_tokens (`>`, `;`, `&&`, `$(...)`, backtick, `||`) → DENY
- T2 pipe_right_deny (`| sh` / `| curl`) → DENY
- T2 pipe > 5 段 → DENY (pipe_too_deep)
- T3 dangerous_cmd / deny_subcommand → DENY
- T4 credential_path / credential_glob / proc_sensitive → DENY (升 tier)
- 黑名单优先白名单 (`rm file` 即使 cat-allowed 也 DENY)
- 空 cmd / 缺 config / kill switch (env var 或 file flag) → ASK
- 异常 fail-closed → ASK

### `src/ssh_sudo_allowlist.py` (ssh+sudo 非写入 allowlist)

- 非 ssh/sudo cmd → GOVERNOR (不归本层)
- Config 缺失 → GOVERNOR (fail-safe)
- allow_prefixes 命中 (sudo tail / sudo systemctl status / 远程 ls) → ALLOW
- Blacklist (credential_path / dangerous_cmd / deny_subcommand) → DENY
- deny_tokens 优先于一切 → DENY
- `ssh host 'inner_cmd'` 拆解, inner 走黑/白检查
- pipe 单层 + 右侧不在 pipe_right_deny → 允许走第一段决策
- pipe > 5 段 → DENY (too_many_pipes)
- 不命中 → GOVERNOR (M5 / HC-PRE-2 fail-safe)
- `check_with_audit` 写 jsonl 到 `AUDIT_DIR/ssh_sudo_audit_YYYYMMDD.jsonl`

## 未覆盖 (下一波 layer)

按 ROI 排序的后续目标:

### Layer 2 — boundary mock (集成式)

- **`src/master/server.py`** — 3000+ 行单文件 master HTTP/WS, 用 `urllib.request`
  打 in-process `ThreadingHTTPServer` 起的实例, 内部 sqlite 用 `:memory:`,
  fs 用 `tmp_path`. 不 mock master 内部, mock 它的外部依赖
- **`pre_mcp/tools.py`** — caller-id binding 校验 + loopback 强制 + 跨 node
  `read_pane` 拒绝, mock `master_client` facade
- **`src/hook.py` / `src/governor.py`** — fake `claude -p` subprocess (用 echo +
  预编 JSON verdict), 测决策链 4 步顺序 + fail-safe = ask
- **`src/analyzer.py`** — stop hook 检测 finding / cycle alert, fixture 喂
  transcript snapshot

### Layer 3 — e2e smoke (慢 + 脆, 不进 CI)

- `scripts/bus_ctl.sh` 起 master + 1 node, MCP 子进程 ping master,
  `send_message` 一来一回, teardown
- 跑在 attached tmux (detached tmux 抓不到 codex/gemini TUI), 所以**只**做本地手敲
  `pre test --e2e`, 不进 CI

### 其他单元可加

- `src/transcript_parser.py` (文本入 → 结构化出)
- `src/drivers/*/pending_parser.py` (pane snapshot → decision)
- `src/freerun_intervention_loop.py`
- `src/runtime/` 生命周期评估器
- `src/notify.py` / `src/reporter.py` 路由表

## 修改时如何不被锁死

如果改动**有意**调整行为 (不是 regression):

1. 改 `src/<module>.py`
2. 跑 `uv run --with pytest pytest tests/test_<module>.py -v` 看哪些 case red
3. 判断 red 的 case 是否反映新意图; 是 → 改 test 断言, 否 → 改回代码
4. **每个改动的 test 断言要在 PR / commit message 里解释**

如果改动**无意**改了边界 (regression):

- test red 立刻发现, 不会沉默 ship

## 隔离守护 (备选, 未启用)

如果未来想加一道**只读守护** — 跑测试前后比对真实 `~/.pre/env` /
`pre_rule` / `pre_log` 的 mtime, 任何 test 不小心写到真实路径整个 session
fail — 在 `conftest.py` 加一个 session-scope autouse fixture 即可. 当前
diff-exit-0 已经手工验证过, 暂不引入.
