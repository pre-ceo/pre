# gover_review agent — 项目级规则

governor LLM 决策时会读这个文件. 给 codex / claude 子进程看的硬性约束.

## 安全 (硬约束)

- **不允许** 直接编辑 `~/cursor/pre/src/rules.py` / `~/cursor/pre/src/governor.py` /
  `~/cursor/pre/src/prehook_evaluator.py` — 这些是 PreToolUse 决策链, 改了会改变
  全机器所有 agent 行为. 这个 agent 只输出 patch 草案让用户 apply.

- **不允许** 调 `mcp__pre__send_message` 给除自己外的 agent. 防止跨仓污染.

- **不允许** 修改 `~/.pre/env` / `~/.pre/data/master.db` — token & bus state 不归这
  agent 管.

## 允许 (常规操作)

- 读 `~/cursor/pre_rule/logs/*.jsonl` — ask 抽取数据源
- 读 `~/.claude/projects/**/*.jsonl` — transcript 反查
- 读 `~/cursor/pre/src/rules.py` / `~/cursor/pre/src/governor.py` — 看现有规则, 输出 diff 时需要
- 写 `~/.pre/internal_agents/gover_review/pre/findings/INFO-*.md` — INFO finding
- 写 `~/cursor/pre/dev-workflow/findings/*.md` — 改进报告
- 写 `~/.pre/state/gover_review.json` — state 持久化
- 调 `codex -p` subprocess — review 算法

## 决策语境

如果 governor 看到本 agent 跑 `git diff src/rules.py` / `cat src/rules.py` 这类只读
查看 — **应放行**, 是 agent 写 patch 草案必须读现有规则.
