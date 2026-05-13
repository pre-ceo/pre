#!/usr/bin/env bash
# check_tmux_version.sh — fail-closed 校验 tmux >= 2.4
# 2.4 是 'tmux has-session -t =name' exact-match 的最低版本; <2.4 时
# spawn 的 prefix-bug (016 sister: pre vs pre_ui 同前缀场景) 防不了.
# 跟 spawn_agent.sh REMOTE_SCRIPT inline 校验等价 (line 293-298).
set -e
v=$(tmux -V 2>/dev/null | awk '{print $2}' | sed 's/^next-//;s/[a-z]*$//')
maj=$(echo "$v" | cut -d. -f1)
min=$(echo "$v" | cut -d. -f2)
if [ -z "$maj" ] || [ -z "$min" ]; then
    echo "check_tmux_version: cannot parse tmux -V output ($v)" >&2
    exit 2
fi
if [ "$maj" -ge 3 ] || { [ "$maj" -eq 2 ] && [ "$min" -ge 4 ]; }; then
    exit 0
fi
echo "check_tmux_version: tmux $v < 2.4 (=name exact-match unavailable)" >&2
exit 1
