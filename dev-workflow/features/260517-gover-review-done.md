# gover_review — 周期审查 governor ask, 输出规则改进 proposal

## 动机

PreToolUse 决策链第 3 级 (`src/governor.py` 调 claude -p) 把灰区 Bash 命令交给 LLM
判 ALLOW/ASK. 用户日常实战里, governor 频繁返 ASK 打断流程 — 但相当一部分 ASK 其实
"应该 allow" (e.g. 同一个供应链命令重复审, 同一个 cwd 内某个 curl 模式实际安全).

历史上没有系统化方式把这些 "重复 ASK" 反馈成规则改进. 用户每次手动 confirm,
经验不沉淀; 改 `src/rules.py` 白名单 / `pre_rule/global.md` 规则也得手动 grep 日志看
近期 ASK 分布. 这个 feature 把这条反馈环路自动化:

- cron 每 4h 审查一次本周期 governor ASK 历史
- 用 codex -p 重审, 输出可落地的规则 patch 草案
- 用户审批 (B2 文件协议), agent 写改进报告
- 用户手动 apply patch — 规则改进入仓

## 用户决策固化

设计前对齐的 5 个决策点 (用户拍板):

| key | 选项 | 决策 |
|---|---|---|
| A | LLM provider | **codex -p** (与现有 claude provider 互为校验) |
| B | 用户审批通道 | **B2 文件协议** — INFO finding 用户填一行, agent watch sha256 |
| C | cycle 周期边界 | **wall-clock 对齐** (00/04/08/12/16/20 UTC) |
| D | patch 落地 | **只输出草案, 不自动 apply** |
| E | 改进报告位置 | **`dev-workflow/findings/YYMMDD-cycle-N.md`** |
| F | 安装通道 | **pre install / pre update 自动注册并启动** |

加上 3 个微决策:

| key | 决策 |
|---|---|
| J | jsonl 邻居窗 ±5min, transcript 反查 ±10 条 |
| K | transcript 反查 `~/.claude/projects/<encoded_cwd>/<session_prefix>*.jsonl` |
| L | cache key 不含 cwd 是潜在 bug — 不在 feature 范围, codex risk_note 提示 |

## Layer A/B/C — 上下文 vs 落地约束

摸底 `src/governor.py` 发现 governor 真实决策时**比想象的字段多**: 拿到 cmd /
cwd / 最近 5 条 transcript 摘要 (200 字/条) / system+global+project rules.md.
而 `src/rules.py` 字面白名单只看 cmd 字符串. 这意味着 review agent 拉的上下文
要跟 "落地后真实可见的字段" 对齐, 否则规则跨 cwd 误放行 / 失效.

三层约束:

| Layer | 决策者 | 看 cmd | 看 cwd | 看 history | 看 rules.md |
|---|---|:---:|:---:|:---:|:---:|
| **A** | review agent (本 feature) | ✓ | ✓ | ✓ 任意多 | ✓ |
| **B** | governor LLM (claude -p) | ✓ | **✓** | **✓ 5 条 200 字** | ✓ |
| **C** | 白名单字面匹配 (rules.py) | ✓ | ✗ | ✗ | ✗ |

每个 proposal **强制带 `target_layer ∈ {B, C}`**, codex prompt 硬约束:

- target_layer=C: rule_patch_draft 只能依赖 cmd 字符串模式
- target_layer=B: 可引用 cmd + cwd + 历史关键词 (governor LLM 都能看到)
- 一律不引用 transcript 细节 / mode / env / agent_id (governor 看不到)

## 状态机 (4h wall-clock 对齐)

```
IDLE                                              (pending_finding_path == None)
   | should_run_cycle() → (since, until)
   v
REVIEWING                            extract.py + reviewer.py per-ask codex
   | review_batch 返 N 个 proposal
   v
WAITING_USER                          (pending_finding_path != None)
   | cron tick → should_run_cycle() → None (skip)
   | user 编辑 finding → sha256 变 → parse_answers
   v
COMPLETE                              format_report + write 到 dev-workflow/findings
   | complete_cycle(until=*) — cycle_n+=1, last_cycle_end_ts=until, 清 pending
   v
IDLE → 立即下一轮 (吃 WAITING_USER 期间累积的 ask)
```

wall-clock 对齐: `floor_to_period(now, 14400)` 利用 epoch 1970-01-01 整除性质,
边界自然落 00/04/08/12/16/20 UTC. 关机 8h 重启 → 一次窗 `[08:00, 16:00)` 完整审,
不补跑 N 次.

## 模块布局

```
src/gover_review/
├── __init__.py
├── extract.py           U1  jsonl 抽取 + 上下文打包 (邻居 + transcript)
├── reviewer.py          U3  codex -p subprocess + proposal schema + fallback
├── state.py             U4  状态机 + 原子写 state.json
├── reporter.py          U6  finding 写入 + sha256 watcher + 报告生成
├── install_agent.py     U2  workdir 模板 copy helper
└── cron_install.py      U5  schedules.json idempotent merge

scripts/gover_review/
├── cron_trigger.sh      U5  cron cmd 入口 — tmux 探测 + spawn
└── templates/
    ├── agent_config.json   mode=supervised, cli=claude, role=gover_review
    ├── next.md             agent 进 tmux 后的工作循环指令 (状态机 + Layer 约束)
    └── rules.md            governor 决策时的硬约束 (禁改 rules.py 等)

scripts/install_gover_review.py   U7  4 步串联 install entry

tests/gover_review/      150 → 302 (+152 case)
├── test_extract.py            29
├── test_reviewer.py           29
├── test_state.py              27
├── test_reporter.py           31
├── test_install_agent.py      10
├── test_cron_install.py       16
└── test_installer.py          10
```

运行时 (install 时建, 不入 git):
```
~/.pre/internal_agents/gover_review/    agent cwd
├── pre/
│   ├── agent_config.json
│   ├── next.md
│   ├── rules.md
│   └── findings/
│       ├── INFO-gover-improve-cycle-N.md    (WAITING_USER 时存在)
│       └── processed/                       (用户答完移这)
└── .claude/settings.json                    (pre_init 写, 接 hook)

~/.pre/state/gover_review.json               cycle 状态机持久化
pre_rule/cron/schedules.json                 entry id=gover-review-4h merge
pre_rule/agents/Users-<your-user>-.pre-internal_agents-gover_review/
└── agent_pointer.json                       pre_init 写 (encoded cwd)
```

## 数据流

```
master cron (30s tick, src/master/cron.py)
   │ 14400s interval, type=interval, target=local
   v
scripts/gover_review/cron_trigger.sh
   │ tmux has-session "=gover_review"?
   ├── 在 → silent skip (现有 agent watch 中)
   └── 不在 → exec scripts/spawn_agent.sh gover_review
                 │ pre_rule/agents/<encoded>/agent_pointer.json 反查
                 │ tmux new-session + claude (driver hook 走 PreToolUse)
                 v
       agent 进 tmux, claude 读 ~/.pre/internal_agents/gover_review/pre/next.md
       │
       v
    [REVIEWING] python3 -m gover_review.extract --since ... --until ...
       │ 读 pre_rule/logs/pre_hook_YYYYMMDD.jsonl
       │ 反查 ~/.claude/projects/<encoded_cwd>/<session>*.jsonl
       │ → ask_entries with Layer A context
       v
       per-ask codex exec --skip-git-repo-check '<prompt>'  (reviewer.py)
       │ rc=0 → JSON proposal / rc!=0 → keep_ask fallback
       v
       reporter.write_finding(workdir, cycle_n, proposals)
       │ 写 pre/findings/INFO-gover-improve-cycle-N.md + sha256
       v
    [WAITING_USER] state.json: pending_finding_path = INFO path
       │ agent polling 30s sha256 vs original
       │ user 编辑 finding 加 accept/reject/modify/skip
       v
       reporter.parse_answers(finding) → {1: accept, 2: reject, ...}
       │
       v
    [COMPLETE] reporter.write_report(dev-workflow/findings, ...)
       │ 写 YYMMDD-cycle-N.md (summary + 详情 + apply checklist)
       │ move_to_processed(finding)
       │ state.complete_cycle(until=*)
       v
    [IDLE → 下一轮] should_run_cycle() 立即检查 — 吃 pending 期间累积的 ask
```

## 主要设计决策

### codex 调用 fail-safe

`reviewer.py:_run_codex` 复用 `governor.py` 的 `source ~/rule.sh && codex exec
--skip-git-repo-check '<prompt>'` 模式 (PATH 继承 / nvm 加载一致 — 见 memory
`feedback_governor_subprocess_path.md`).

所有 fail-path 一律走 `_fallback_keep_ask`, **不丢条目**:

| 触发 | 返回 |
|---|---|
| codex 未装 (FileNotFoundError, rc=127) | keep_ask + risk_note 标 codex_missing |
| timeout (rc=124) | keep_ask + risk_note 标 timeout |
| rc != 0 | keep_ask + risk_note 含 stderr |
| 输出非 JSON | keep_ask + risk_note 标 parse_error |
| 输出非 dict | keep_ask + risk_note 标 non-dict |
| target_layer ∉ {B, C} | 兜底改 B (保守) |
| action ∉ enum | 兜底改 keep_ask |

### per-ask 调用 vs batch

per-ask: 一条 fail 不带其他 + 单 proposal context 集中. trade-off 是 N 次 codex
启动 (4h 窗内 ask 数实测 6 个量级, 不是性能瓶颈).

### state.json 原子写

`tempfile.mkstemp(dir=parent) + os.replace`. raise 时 unlink tmp 不留半文件.
单测 `test_save_atomic_failure_does_not_corrupt_existing` 用 monkeypatch 强制
`os.replace` raise, 验证原文件不损坏 + tmp 不残留.

### 短命 agent + tmux session 探测

cron `every_seconds=14400`. 每次触发 trigger.sh:

- 已有 tmux session `gover_review` → silent skip (上一个 agent 还在 WAITING_USER
  watch 文件, 或刚 spawn 没退)
- 不存在 → exec spawn_agent.sh, 起新 agent

不依赖 mcp send_message dance (cron daemon role=hook 没 agent identity, 跑 send_message
要么开 master HTTP API 给 hook role, 要么 fake identity). agent 自己进 tmux 后
read next.md 启动 review cycle — 语义等价 "cron 激活 agent".

### `agent_config.mode` vs `cfg.mode` 区分

两个 mode 字段历史上容易混:

- `pre_rule/config.json::mode` (HookConfig.mode, 全机器级) — observe / **enforce**.
  当前用户机器已经 enforce, 本 feature 不改
- `<cwd>/pre/agent_config.json::mode` (per-agent) — supervised / autonomous / freerun.
  gover_review 用 **supervised** — autonomous 下 ASK→DENY 会阻断 review (rules.md
  里写了 codex 等是 allow, 但边界 case 仍可能撞 ASK, supervised 让用户手动批)

### Layer C / B / A 三层约束写进 codex prompt

`reviewer._build_prompt` 末尾硬约束:
> target_layer=C: 只能依赖 cmd 字符串模式; cwd/history 不可引用
> target_layer=B: 可引用 cmd+cwd+历史关键词; 不引用 transcript 细节/mode/env

加 review-agent 看到的丰富 context (jsonl 邻居 + transcript 反查) 只用来**帮 codex
理解**, 不直接进 patch 草案. patch 一旦落地, 真实 governor 看到的字段只有 Layer
B/C 范围.

### Cache key cwd-leak 不在 feature 范围

`src/cache.py:cache_key` 只 hash `(tool_name, command)`, 不含 cwd. 同一 cmd 跨 cwd
复用 verdict — 但 governor 决策时其实**看了** cwd. 意味着 cache 命中的 verdict 可能
基于另一个 cwd 的判断. 这是潜在 bug, 但不在本 feature 范围. codex 在 risk_note 里
若检测到 "同一 cmd 在不同 cwd 决策不一致" 应提示, 让用户考虑修 cache_key 含 cwd.

### B2 文件协议解析 robustness

`reporter._ANSWER_RE = r"^\s*(accept|reject|skip|modify:.*)\s*$"` (IGNORECASE):

- 行级 anchor — Q 段里 "是否 accept?" 文本不会被误识
- A 段在遇到下一个 `### ` 标题时结束 — 不读到下个段
- HTML 注释跳 (占位 `<!-- ... -->` 不被识为答案)
- 第一行匹配即取 — 用户多打几行 ignore 后续

## 各 unit 验证

| Unit | 文件 | LOC | Case | 关键验证 |
|---|---|---|---|---|
| U1 | `extract.py` | 227 | 29 | 跨日 jsonl + transcript reverse-lookup + 半开区间 + cmd helper for non-Bash |
| U2 | `install_agent.py` + 3 模板 | 75 + 模板 | 10 | 模板字段 schema + force=True 覆盖 + 幂等 |
| U3 | `reviewer.py` | 198 | 29 | 注入式 runner mock + Layer 约束写进 prompt + 7 类 fallback |
| U4 | `state.py` | 145 | 27 | wall-clock 4h 边界对齐 + 原子写 + 4 状态转移 + 整链 integration |
| U5 | `cron_install.py` + `cron_trigger.sh` | 86 + 24 | 16 | schedules.json idempotent merge + 容错 (坏 json/非 dict/缺 key) + trigger.sh 存在 |
| U6 | `reporter.py` | 248 | 31 | sha256 sentinel + parse_answers 9 种边界 + wait sleeper 注入 + 端到端流程 |
| U7 | `install_gover_review.py` + install.sh/pre_update.py | 156 + ~13 | 10 | 4 步串联 + 幂等 + fail-safe (任一步缺都不 raise) |

**全套 302/302 通过** (原 150 + 本 feature 152), 0.47s 跑完.

真实 jsonl smoke (`uv run python3 -c '...'` 跑 extract.extract 在 `~/cursor/pre_rule/logs/pre_hook_20260516.jsonl`):
- 全天 6 个 ask (4 governor_no_cache + 2 governor)
- 第一条 ask 拉到 10 邻居 jsonl + 20 transcript excerpt

## 未覆盖 / 遗留

| 项 | 状态 |
|---|---|
| 真跑 codex 端到端 (本机要装 codex CLI + OpenAI quota) | install 后 cron 4h 后自然触发首轮, 或 install_gover_review.py 异步 trigger 立即跑 |
| cache.py cwd-leak 修法 | out of scope, codex risk_note 提示, 单独 feature 处理 |
| 模板版本管理 (用户改了 next.md 又升级) | force=True 直接覆盖. memory 提示用户不该改, 改了 install/update 丢 |
| 真实 transcript 反查 (cwd encoding 边界 case) | encode 用 `cwd.replace("/", "-")`, 跟 `~/.claude/projects/` 实际目录名一致已验证 |
| 跨 cycle 用户长时间不答 | state 留 pending, cron 一直 skip; 没自动催/超时 — 用户主动答触发恢复 |
| WAITING_USER 期间 agent 进程意外退 (tmux kill / 机器重启) | 下次 cron tick 重新 spawn, agent 起来读 state pending_finding_path 非空 → 进 watch (待 U6 next.md 指令 step 1 落实) |
| ask 数巨大 (e.g. 4h 内 100 条) | per-ask 顺序调 codex, ~10s/条 = 1000s. 实测 4h 内 6 条, 不是瓶颈; 真需要再加 batch 模式 |
| `agent_pointer.json` encoded path 含 `.pre` 起头的 dot | spawn_agent.sh `dir.replace("/","-")` 不特殊处理 dot, 应该 work; install 后验 |

## 相关文件 / 接入点

| 文件 | 角色 |
|---|---|
| `pre_rule/logs/pre_hook_YYYYMMDD.jsonl` | ask 数据源 (decision + source + cwd + reason) |
| `~/.claude/projects/<encoded_cwd>/<uuid>.jsonl` | transcript 反查源 |
| `pre_rule/cron/schedules.json` | cron entry 注入位置 |
| `~/.pre/state/gover_review.json` | cycle 状态机持久化 |
| `~/.pre/internal_agents/gover_review/pre/findings/` | INFO finding 写入 (WAITING_USER) |
| `dev-workflow/findings/YYMMDD-cycle-N.md` | 改进报告归档 (本 feature 新建子目录) |
| `src/governor.py` | codex subprocess 调用模式参照 |
| `src/master/cron.py` | cron loop 30s tick + interval type 首次立即触发 |
| `scripts/spawn_agent.sh` | trigger.sh 调用入口 (反查 pointer + tmux + master rediscover) |
| `scripts/pre_init.py` | install_gover_review.py 调用 (写 pointer + .claude/settings.json) |
