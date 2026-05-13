#!/bin/bash
# tmux_startup.sh — sourced by spawn_agent.sh (LOCAL_TMUX_RC fallback chain end).
# spawn_agent.sh 找的是历史名 tmux_startup.sh, 实际 fallback 逻辑在 spawn.rc 同目录.
# user 部署版应放 pre_rule/tmux_startup.sh (优先于此).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/spawn.rc" ] && source "$SCRIPT_DIR/spawn.rc"
