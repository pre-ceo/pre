#!/usr/bin/env bash
# scripts/gover_review/cron_trigger.sh — master cron 每 4h 触发的入口
#
# 行为:
#   1. 已有 tmux session=gover_review → 现有 agent 在 watch user 或刚跑过, silent skip
#   2. 不在 → 调 scripts/spawn_agent.sh gover_review
#
# 依赖: pre init 已经把 ~/.pre/internal_agents/gover_review 注册到 pre_rule/agents/.
# 否则 spawn_agent.sh 反查 pointer 失败 → exit 3. U7 install.sh 负责 init.
#
# master cron 用 asyncio.create_subprocess_exec (不走 shell), schedules.json 的 cmd
# 必须传绝对路径. install 时填.

set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PRE_ROOT="$(cd "$_SCRIPT_DIR/../.." && pwd)"

SESSION="gover_review"
LOG_DIR="${PRE_LOG_DIR:-$(dirname "$_PRE_ROOT")/pre_log}/gover_review"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/cron_trigger_$(date -u +%Y%m%d).log"

_ts="$(date -u +%FT%TZ)"

if tmux has-session -t "=$SESSION" 2>/dev/null; then
    echo "$_ts [cron] tmux session '$SESSION' exists; skip spawn" >> "$LOG_FILE"
    exit 0
fi

echo "$_ts [cron] spawning $SESSION" >> "$LOG_FILE"
exec bash "$_PRE_ROOT/scripts/spawn_agent.sh" "$SESSION" >> "$LOG_FILE" 2>&1
