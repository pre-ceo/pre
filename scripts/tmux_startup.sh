#!/bin/bash
# tmux_startup.sh — sourced by spawn_agent.sh (LOCAL_TMUX_RC fallback chain end).
# spawn_agent.sh 找的是历史名 tmux_startup.sh, 实际 fallback 逻辑在 spawn.rc 同目录.
# user 部署版应放 pre_rule/tmux_startup.sh (优先于此).

# ~/.pre/rc (user init: proxy / PATH / nvm) — 先 source, 给 spawn.rc 的 egress 校验提供前置
if [ -f "$HOME/.pre/rc" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$HOME/.pre/rc"
    set +a
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/spawn.rc" ] && source "$SCRIPT_DIR/spawn.rc"
