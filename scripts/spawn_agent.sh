#!/usr/bin/env bash
# scripts/spawn_agent.sh — 拉起一个 agent (tmux + claude code, 注册到总线)
#
# 用法:
# bash scripts/spawn_agent.sh <agent_id> [tmux_session_override]
#
# agent_id e.g. local.cli-claude-code-local.pre — 由 pre-init 初始化时产生.
# 本脚本从 pre_rule/agents/<dir>/agent_pointer.json 反查 cwd, 不再假定 PRE_AGENT_HOME/<project>.
# 必须先用 scripts/pre_init.py 在 agent cwd 初始化, 否则 pointer 不存在会失败.
# 起完后 POST /api/v1/nodes/<node>/rediscover 让 node 重发现 + 注册.
#
# 依赖: tmux, curl, claude (PATH), python3 (解析 pointer / agent_config.json)

set -euo pipefail

ARG1="${1:-}"

if [[ -z "$ARG1" || "$ARG1" != *.* ]]; then
    echo "[spawn_agent] 用法: $0 <agent_id> [tmux_session_override]" >&2
    echo "  agent_id 形如: local.cli-claude-code-local.pre (3 段, '.' 分隔)" >&2
    echo "  必须先用 'python3 scripts/pre_init.py <cwd>' 注册 agent" >&2
    exit 2
fi

AGENT_ID_FULL="$ARG1"
PROJECT="${ARG1##*.}"

# pre_rule root: PRE_RULE_ROOT > pre repo sibling (与 src/config.py:RULE_ROOT 一致)
_SCRIPT_DIR_INIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PRE_ROOT_INIT="$(dirname "$_SCRIPT_DIR_INIT")"
RULE_ROOT="${PRE_RULE_ROOT:-$(dirname "$_PRE_ROOT_INIT")/pre_rule}"

# 反查 pointer 找 cwd: 遍历 pre_rule/agents/*/agent_pointer.json 匹配 agent_id 字段
POINTER_CWD=""
RULE_AGENT_DIR=""
if [[ -d "$RULE_ROOT/agents" ]]; then
    for pdir in "$RULE_ROOT/agents"/*/; do
        [[ -d "$pdir" ]] || continue
        ptr="${pdir}agent_pointer.json"
        [[ -f "$ptr" ]] || continue
        _vals=$(python3 -c "
import json
try:
    d = json.load(open('$ptr'))
    print(d.get('agent_id', ''))
    print(d.get('cwd', ''))
except Exception:
    pass
" 2>/dev/null) || continue
        _aid=$(printf '%s\n' "$_vals" | sed -n '1p')
        _cwd=$(printf '%s\n' "$_vals" | sed -n '2p')
        if [[ "$_aid" == "$AGENT_ID_FULL" ]]; then
            POINTER_CWD="$_cwd"
            RULE_AGENT_DIR="${pdir%/}"
            break
        fi
    done
fi

if [[ -z "$POINTER_CWD" ]]; then
    echo "[spawn_agent] FATAL: agent_pointer.json for agent_id='$AGENT_ID_FULL' not found" >&2
    echo "  searched: $RULE_ROOT/agents/*/agent_pointer.json" >&2
    echo "  run 'python3 $_PRE_ROOT_INIT/scripts/pre_init.py <cwd>' first" >&2
    exit 3
fi

PROJECT_DIR="$POINTER_CWD"

# tmux_session: $2 override > cwd/pre/agent_config.tmux_session > PROJECT
if [[ -n "${2:-}" ]]; then
    TMUX_SESSION="$2"
elif [[ -f "$PROJECT_DIR/pre/agent_config.json" ]]; then
    _ts=$(python3 -c "
import json
try:
    d = json.load(open('$PROJECT_DIR/pre/agent_config.json'))
    print(d.get('tmux_session', '') or '')
except Exception:
    print('')
" 2>/dev/null || echo "")
    TMUX_SESSION="${_ts:-$PROJECT}"
else
    TMUX_SESSION="$PROJECT"
fi

MASTER_URL="${PRE_MASTER_URL:-http://127.0.0.1:19500}"
NODE_ID="${PRE_NODE_ID:-local}"
TOKEN="${PRE_SECRET:-fnpre}"

# tmux startup rc resolution (source rule.sh + JP egress 验证)
# 优先序: $RULE_ROOT/tmux_startup.sh (env-first via RULE_ROOT line 31) > <pre>/scripts/tmux_startup.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_resolve_local_rc() {
    if [ -f "$RULE_ROOT/tmux_startup.sh" ]; then
        echo "$RULE_ROOT/tmux_startup.sh"
    else
        echo "$SCRIPT_DIR/tmux_startup.sh"
    fi
}
LOCAL_TMUX_RC=$(_resolve_local_rc)

# M1 (P0, agent-security R3) — fail-closed tmux >=2.4 校验 (=name exact-match 必需)
# 本地校验; 远端校验 inline 在 REMOTE_SCRIPT 里 (跟 spawn 共用 ssh session, 1 round-trip)
if ! bash "$SCRIPT_DIR/check_tmux_version.sh" >&2; then
    echo "[spawn_agent] FAIL-CLOSED: 本地 tmux 不满足 >=2.4 (=name exact-match 不可用)" >&2
    exit 8
fi

# 颜色 (cyan/yellow/blue/magenta, 不用红绿)
C_CYAN='\033[36m'
C_YELLOW='\033[33m'
C_BLUE='\033[34m'
C_MAGENTA='\033[35m'
C_RESET='\033[0m'
info()  { printf "${C_BLUE}[spawn]${C_RESET} %s\n" "$*"; }
ok()    { printf "${C_CYAN}[ok]${C_RESET} %s\n" "$*"; }
warn()  { printf "${C_YELLOW}[warn]${C_RESET} %s\n" "$*"; }
emph()  { printf "${C_MAGENTA}[!]${C_RESET} %s\n" "$*"; }

# ============================================================
# G10 audit log
# 路径: pre_log/security/remote_node_audit_YYYYMMDD.jsonl chmod 600
# ============================================================
AUDIT_DIR="${PRE_LOG_DIR:-$(dirname "$_PRE_ROOT_INIT")/pre_log}/security"
mkdir -p "$AUDIT_DIR" 2>/dev/null && chmod 700 "$AUDIT_DIR" 2>/dev/null || true
AUDIT_FILE="$AUDIT_DIR/remote_node_audit_$(date +%Y%m%d).jsonl"

audit() {
    # audit <action> <result> [extra_json_kv ...]
    # extra: key=value pairs, value 自动 json-encode
    local action="$1" result="$2"; shift 2
    local extras=""
    for kv in "$@"; do
        local k="${kv%%=*}" v="${kv#*=}"
        # value 转义 (引号 / 反斜杠 / 控制字符)
        v=$(printf '%s' "$v" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '"?"')
        extras+=", \"$k\": $v"
    done
    local entry="{\"ts\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"action\": \"$action\", \"result\": \"$result\", \"agent_id\": \"$AGENT_ID_FULL\", \"target_node\": \"$TARGET_NODE\", \"project\": \"$PROJECT\"$extras}"
    printf '%s\n' "$entry" >> "$AUDIT_FILE"
    chmod 600 "$AUDIT_FILE" 2>/dev/null || true
}

# Finding HIGH 写到项目 pre/findings/
# needs_wire: 判断 .claude/settings.json 是否需要 wire (写入/重写 hook block).
# 返 0 (true) = 需要写; 返 1 (false) = 已完整可 skip.
# [ dispatch fix] 修原 line 187 仅判断 [! -f] 跳过空 {} 文件 bug.
# HC-PRE-1 stdlib only (用 python3 不引 jq, 远端可能没装 jq).
# HC-PRE-2 fail-safe (损坏 json fall-through 到 wire, 旧坏文件先 backup).
needs_wire() {
    local f="$1"
    [ ! -f "$f" ] && return 0  # case 5: 不存在 → wire
    python3 -c "
import json, sys
try:
    d = json.load(open('$f'))
    h = d.get('hooks', {})
    if not isinstance(h, dict) or not h.get('PreToolUse') or not h.get('Stop'):
        sys.exit(0)
    pre = h.get('PreToolUse')
    stop = h.get('Stop')
    if not isinstance(pre, list) or not pre or not isinstance(stop, list) or not stop:
        sys.exit(0)
    sys.exit(1)
except (OSError, ValueError):
    sys.exit(0)
" 2>/dev/null && return 0 || return 1
}

write_finding_high() {
    # write_finding_high <title_slug> <body>
    local slug="$1" body="$2"
    local fdir="$PROJECT_DIR/pre/findings"
    mkdir -p "$fdir" 2>/dev/null
    local fpath="$fdir/HIGH-$slug-$(date +%Y%m%d%H%M%S).md"
    cat > "$fpath" <<FEOF
# HIGH: $slug

ts: $(date -u +%Y-%m-%dT%H:%M:%SZ)
agent_id: $AGENT_ID_FULL
target_node: $TARGET_NODE
project: $PROJECT

$body

---
来源: spawn_agent.sh fail-closed ( Phase A)
FEOF
    emph "finding HIGH 已写 $fpath"
}

# ============================================================
# 入口层路由
# [agent-research-only hack 自 待 ≥3 用例升级通用 registry 字段,
# 不删除原 hack 而是追加新分支 — 见 agent-gov verdict G9]
# ============================================================
ALLOWED_NODES_JSON='["local"]'
REMOTE_ONLY="false"
FALLBACK_POLICY="fail-closed"

# 读 agent_config.json 3 字段 (若 PROJECT_DIR 已有 config)
if [[ -f "$PROJECT_DIR/pre/agent_config.json" ]]; then
    _cfg_out=$(python3 - <<PYEOF 2>/dev/null || echo ":::"
import json, sys
try:
    with open("$PROJECT_DIR/pre/agent_config.json") as f:
        d = json.load(f)
except Exception:
    d = {}
allowed = d.get("allowed_nodes")
if not isinstance(allowed, list) or not allowed:
    allowed = ["local"]
remote_only = bool(d.get("remote_only", False))
fallback = d.get("fallback_policy", "fail-closed")
# fail-closed 不可关 (D2 hard)
if fallback != "fail-closed":
    fallback = "fail-closed"
print(json.dumps(allowed) + ":::" + ("true" if remote_only else "false") + ":::" + fallback)
PYEOF
    )
    if [[ "$_cfg_out" != ":::" ]] && [[ -n "$_cfg_out" ]]; then
        ALLOWED_NODES_JSON="${_cfg_out%%:::*}"
        _rest="${_cfg_out#*:::}"
        REMOTE_ONLY="${_rest%%:::*}"
        FALLBACK_POLICY="${_rest#*:::}"
    fi
fi

# 决策 target_node = allowed_nodes[0] (Phase A 仅取头, 不做选择)
TARGET_NODE=$(printf '%s' "$ALLOWED_NODES_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0])' 2>/dev/null || echo "local")
# AGENT_ID_FULL 已从入参 ARG1 拿到 (line 25), 不重拼; M7-1 invariant 下方校验前缀.

# M7-1 invariant ( step 1, agent-security M7 第 5 次治理债务实证):
# agent_id 前缀 (第一 . 之前) 必等于 target_node, 防 future regression / 入侵伪造.
# 此处 AGENT_ID_FULL 来自用户输入 (ground truth), TARGET_NODE 来自 agent_config — 二者必须一致.
_AGENT_PREFIX="${AGENT_ID_FULL%%.*}"
if [[ "$_AGENT_PREFIX" != "$TARGET_NODE" ]]; then
    emph "[M7-1 invariant] FAIL: agent_id 前缀 '$_AGENT_PREFIX' != target_node '$TARGET_NODE'"
    audit "spawn_agent_id_invariant" "failed" "agent_id=$AGENT_ID_FULL" "target_node=$TARGET_NODE" "agent_prefix=$_AGENT_PREFIX"
    write_finding_high "spawn-agent-id-mismatch" \
        "M7-1 invariant violated: spawn agent_id 前缀 '$_AGENT_PREFIX' 不等 target_node '$TARGET_NODE'. 可能 code regression 或入侵伪造. agent-security M7 第 5 次实证. 严禁 spawn 继续."
    exit 8
fi

info "[route] route decision: target_node=$TARGET_NODE remote_only=$REMOTE_ONLY fallback_policy=$FALLBACK_POLICY"

# G5 fail-closed: remote_only=true 但 target_node==local → 拒
if [[ "$REMOTE_ONLY" == "true" ]] && [[ "$TARGET_NODE" == "local" ]]; then
    emph "[guard] FAIL-CLOSED: remote_only=true 但 allowed_nodes[0]=local 矛盾"
    audit "spawn_route_check" "rejected" "reason=remote_only_but_local_target"
    write_finding_high "spawn-config-mismatch" \
        "remote_only=true 但 allowed_nodes[0]=local. 修 agent_config.json 或显式 allow local."
    exit 4
fi

# 远端路径 (Phase A agent-research-only hack: 仅支持 remote-node-root)
if [[ "$TARGET_NODE" != "local" ]]; then
    # [agent-research-only hack 自 ] 硬编码 remote-node-root, 不读 remote_nodes.json 做通用映射
    # 待 ≥3 用例升级 — 见 G9
    if [[ "$TARGET_NODE" != "remote-node" ]]; then
        emph "[phase] 仅支持 target_node=remote-node (agent-research-only hack), 收到 '$TARGET_NODE'"
        audit "spawn_route_check" "rejected" "reason=phase_a_only_remote-node"
        write_finding_high "spawn-unsupported-node" \
            "Phase A 仅支持 remote-node. Phase B 升级触发条件: ≥3 真实 agent_config 用例 + user 显式批."
        exit 5
    fi
    SSH_ALIAS="remote-node-root"
    REMOTE_CURSOR_ROOT="/root/cursor"
    REMOTE_PROJECT_DIR="$REMOTE_CURSOR_ROOT/$PROJECT"

    # G6 spawn 预检: ssh + claude --version (远端独立 OAuth, 不传 key/credential)
    info "[guard] 预检 ssh $SSH_ALIAS claude --version"
    if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$SSH_ALIAS" \
            "bash -lc 'command -v claude >/dev/null && claude --version'" </dev/null >/dev/null 2>&1; then
        emph "[guard] FAIL-CLOSED: ssh $SSH_ALIAS 不可达或远端无 claude"
        audit "spawn_remote_precheck" "failed" "ssh_alias=$SSH_ALIAS" "reason=ssh_or_claude_unreachable"
        write_finding_high "spawn-remote-precheck-failed" \
            "ssh $SSH_ALIAS claude --version 失败. 可能: ssh tunnel down / 远端无 claude / OAuth 未配置. 严禁 silent fallback 本地 (G5)."
        exit 6
    fi
    audit "spawn_remote_precheck" "ok" "ssh_alias=$SSH_ALIAS"

    # 模板硬编码无 user input 拼接 (G3): PROJECT 走 printf %q 转义
    PROJECT_Q=$(printf '%q' "$PROJECT")
    TMUX_Q=$(printf '%q' "$TMUX_SESSION")
    info "[route] ssh exec spawn on $SSH_ALIAS: project=$PROJECT_Q tmux=$TMUX_Q"

    # 远端: mkdir + 写 agent_config + .claude/settings.json + tmux new-session
    # 注: 远端 pre 路径 /root/workspace/pre (见 pre_rule/remote_nodes.json _comment)
    # M1 (P0, agent-security R3) — 远端 tmux >=2.4 fail-closed inline 校验
    REMOTE_SCRIPT='set -e
_tmux_v=$(tmux -V 2>/dev/null | awk "{print \$2}" | sed "s/^next-//;s/[a-z]*$//")
_maj=$(echo "$_tmux_v" | cut -d. -f1); _min=$(echo "$_tmux_v" | cut -d. -f2)
if ! [ "$_maj" -ge 3 ] && ! { [ "$_maj" -eq 2 ] && [ "$_min" -ge 4 ]; }; then
  echo "REMOTE_TMUX_VERSION_TOO_OLD: $_tmux_v < 2.4"; exit 9
fi
mkdir -p '"$REMOTE_PROJECT_DIR"'/pre '"$REMOTE_PROJECT_DIR"'/.claude
if [ ! -f '"$REMOTE_PROJECT_DIR"'/pre/agent_config.json ]; then
  cat > '"$REMOTE_PROJECT_DIR"'/pre/agent_config.json <<CFGEOF
{
  "mode": "supervised",
  "tmux_session": '"$TMUX_Q"',
  "_remote_spawned_by": "mbpdavis spawn_agent.sh",
  "_phase_a_marker": "agent-research-only hack "
}
CFGEOF
fi
_REMOTE_SET_F='"$REMOTE_PROJECT_DIR"'/.claude/settings.json
_remote_needs_wire() {
  local f="$1"
  [ ! -f "$f" ] && return 0
  python3 -c "
import json, sys
try:
    d = json.load(open(\"$f\"))
    h = d.get(\"hooks\", {})
    if not isinstance(h, dict) or not h.get(\"PreToolUse\") or not h.get(\"Stop\"):
        sys.exit(0)
    pre = h.get(\"PreToolUse\"); stop = h.get(\"Stop\")
    if not isinstance(pre, list) or not pre or not isinstance(stop, list) or not stop:
        sys.exit(0)
    sys.exit(1)
except (OSError, ValueError):
    sys.exit(0)
" 2>/dev/null && return 0 || return 1
}
if _remote_needs_wire "$_REMOTE_SET_F"; then
  [ -f "$_REMOTE_SET_F" ] && cp "$_REMOTE_SET_F" "$_REMOTE_SET_F.bak.$(date +%Y%m%d_%H%M%S)"
  cat > "$_REMOTE_SET_F" <<HOOKEOF
{
  "hooks": {
    "PreToolUse": [{"hooks":[{"type":"command","command":"python3 /root/workspace/pre/scripts/pre_tool_use.py"}]}],
    "Stop":       [{"hooks":[{"type":"command","command":"python3 /root/workspace/pre/scripts/stop_hook.py"}]}]
  }
}
HOOKEOF
fi
if tmux has-session -t '"=$TMUX_Q"' 2>/dev/null; then
  echo "TMUX_EXISTS"
else
  # wrap claude with tmux_startup rc (proxy + JP egress check)
  REMOTE_RC="/root/workspace/pre_rule/tmux_startup.sh"
  [ ! -f "$REMOTE_RC" ] && REMOTE_RC="/root/workspace/pre/scripts/tmux_startup.sh"
  tmux new-session -d -s '"$TMUX_Q"' -c '"$REMOTE_PROJECT_DIR"' "bash -ic \"source $REMOTE_RC && exec claude\""
  sleep 2
  # exact match (=) 防 prefix bug (016 sister); pre vs pre_ui 同前缀场景
  tmux has-session -t '"=$TMUX_Q"' && echo "TMUX_STARTED" || { echo "TMUX_FAIL"; exit 7; }
fi'

    if ! out=$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$SSH_ALIAS" "$REMOTE_SCRIPT" 2>&1); then
        emph "[guard] FAIL-CLOSED: 远端 spawn 失败"
        audit "spawn_remote_exec" "failed" "ssh_alias=$SSH_ALIAS output=${out:0:200}"
        write_finding_high "spawn-remote-exec-failed" \
            "ssh exec spawn 失败. output (首 500): ${out:0:500}"
        # G5: 严禁 silent fallback 本地
        exit 7
    fi
    ok "[route] 远端 spawn ok: $out"
    audit "spawn_remote_exec" "ok" "ssh_alias=$SSH_ALIAS output=${out:0:120}"

    # G4 ack 5s nonce: spawn 后调 master rediscover (target_node), 5s 等 agent 注册
    NONCE="adr012-$(date +%s)-$$"
    info "[guard] rediscover $TARGET_NODE + 5s ack 等 agent 注册 (nonce=$NONCE)"
    curl -sS -X POST "$MASTER_URL/api/v1/nodes/$TARGET_NODE/rediscover" \
        -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
        -d "{\"nonce\":\"$NONCE\"}" >/dev/null 2>&1 || warn "rediscover 调用失败 (可能远端 node 未连)"

    # 5s 内轮询 agent 出现在 registry (HC-G10 例外: 单次 bootstrap 不是周期 polling)
    deadline=$((SECONDS + 5))
    listed="False"
    while [[ $SECONDS -lt $deadline ]]; do
        listed=$(curl -sS -H "Authorization: Bearer $TOKEN" "$MASTER_URL/api/v1/agents" 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(any(a['agent_id']=='$AGENT_ID_FULL' for a in d.get('agents',[])))" 2>/dev/null || echo "False")
        [[ "$listed" == "True" ]] && break
        sleep 1
    done
    if [[ "$listed" != "True" ]]; then
        # G4: 失败 fail-closed kill remote tmux + finding HIGH + alert
        emph "[guard] FAIL-CLOSED: 5s 内 agent $AGENT_ID_FULL 未注册到 master"
        ssh -o ConnectTimeout=5 -o BatchMode=yes "$SSH_ALIAS" "tmux kill-session -t $TMUX_Q 2>/dev/null || true" </dev/null >/dev/null 2>&1
        audit "spawn_remote_ack" "failed" "nonce=$NONCE timeout=5s action=kill_remote_tmux"
        write_finding_high "spawn-remote-ack-timeout" \
            "远端 tmux 已起但 5s 内 agent_id=$AGENT_ID_FULL 未注册到 master. 已 kill remote tmux. 检查 远端 node daemon / ssh tunnel / master ws connection."
        exit 8
    fi
    ok "[route] agent $AGENT_ID_FULL 已注册到总线 (nonce=$NONCE 路径 healthy)"
    audit "spawn_remote_ack" "ok" "nonce=$NONCE"
    exit 0
fi

# 以下为 local 路径 (target_node==local), 走原流程
# 1. 项目目录 + agent_config.json
if [[ ! -d "$PROJECT_DIR" ]]; then
    warn "$PROJECT_DIR 不存在, 仍要创建 agent? 假设是新项目, 建空目录"
    mkdir -p "$PROJECT_DIR"
fi
mkdir -p "$PROJECT_DIR/pre"
if [[ ! -f "$PROJECT_DIR/pre/agent_config.json" ]]; then
    info "写 $PROJECT_DIR/pre/agent_config.json"
    cat > "$PROJECT_DIR/pre/agent_config.json" <<EOF
{
  "mode": "supervised",
  "tmux_session": "$TMUX_SESSION"
}
EOF
fi

# 2. pre_rule/agents/<dir>
mkdir -p "$RULE_AGENT_DIR"
if [[ ! -f "$RULE_AGENT_DIR/agent_config.json" ]]; then
    info "写 $RULE_AGENT_DIR/agent_config.json"
    cat > "$RULE_AGENT_DIR/agent_config.json" <<EOF
{
  "mode": "supervised"
}
EOF
fi

# 2.5 .claude/settings.json (PreToolUse + Stop hook 接入 pre)
# [ dispatch fix] 用 needs_wire() 三态判断 (不存在/缺/损坏 → wire), backup 永留, idempotent.
mkdir -p "$PROJECT_DIR/.claude"
_LOCAL_SET_F="$PROJECT_DIR/.claude/settings.json"
if needs_wire "$_LOCAL_SET_F"; then
    if [[ -f "$_LOCAL_SET_F" ]]; then
        _bak="$_LOCAL_SET_F.bak.$(date +%Y%m%d_%H%M%S)"
        cp "$_LOCAL_SET_F" "$_bak"
        info "备份现有 settings.json → $_bak"
    fi
    info "写 $_LOCAL_SET_F (接 pre hook via shim @ ~/.local/bin)"
    # check shim 已装 (由 scripts/install.sh 一次性装, 不在 spawn 时装).
    if [ ! -f "$HOME/.local/bin/pre-tool-use" ]; then
        emph "[FAIL] shim ~/.local/bin/pre-tool-use 未装; 先跑 'bash $_PRE_ROOT_INIT/scripts/install.sh'"
        exit 9
    fi
    cat > "$_LOCAL_SET_F" <<'EOF'
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "pre-tool-use"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "pre-stop-hook"
          }
        ]
      }
    ]
  }
}
EOF
fi

# 2.6 项目级 pre/rules.md (默认轻量模板, 项目可后续扩)
if [[ ! -f "$PROJECT_DIR/pre/rules.md" ]]; then
    info "写 $PROJECT_DIR/pre/rules.md (默认模板)"
    cat > "$PROJECT_DIR/pre/rules.md" <<'EOF'
# Project Rules

参考 pre_rule/global.md 全局规则; 此处仅定义本项目特化扩展。

## ALLOW (项目自允许)
- 读类: Read/Grep/Glob 在本项目 cwd 内自动 allow (rules.py 已有逻辑)
- 跨项目只读 (例如本项目 GUI / 文档需要参考其他 fn_* 的源码 / API 文档): allow
- 调 master API GET (查询类): /api/v1/{nodes,agents,messages,pending,...}
- tmux capture-pane (查 agent 状态)
- agent_reply.py / list_pending.py / dispatch_inbox.py / decide.py 等 helper (走 master 总线)
- ssh 远程只读 (ls/cat/log/status)

## ASK (项目要求显式确认)
- 写类: Write/Edit/MultiEdit (本项目目录内, governor 通常 allow; 项目认为高敏可改 ASK)
- POST /api/v1/agents/{id}/{send,decide} 远程操控其他 agent (尤其 decide)
- spawn_agent.sh (拉起新 agent, 副作用大)
- npm/pip/cargo install / curl | sh (供应链, 走 governor)

## 项目特化
(各项目按需增补)
EOF
fi

# 3. tmux session
# exact match (=) 防 prefix bug (016 sister, agent-ceo audit 002)
# 旧: -t fn_homelab 在已有 fn_homelab_bpi 时 partial match, 误判跳过启动
# M2 (P1, agent-security R3) — defense-in-depth audit:
# 即使 exact match 通过, 仍 list 同前缀 sister sessions 进 audit log (溯源攻击伪装企图)
_sister=$(tmux list-sessions -F '#{session_name}' 2>/dev/null \
    | awk -v p="$TMUX_SESSION" '$0 != p && index($0, p"_") == 1')
if [ -n "$_sister" ]; then
    audit "tmux_prefix_sister_detected" "warn" \
        "session=$TMUX_SESSION" "sisters=$(echo "$_sister" | tr '\n' ',')"
    warn "[M2] 同前缀 sister sessions: $(echo "$_sister" | tr '\n' ',') (audit logged)"
fi
if tmux has-session -t "=$TMUX_SESSION" 2>/dev/null; then
    ok "tmux session $TMUX_SESSION 已存在, 跳过启动"
else
    # 读 agent_config.json 的 start_command 字段, 否则默认 'claude'
    START_CMD="claude"
    if command -v python3 >/dev/null 2>&1; then
        custom=$(python3 -c "import json,sys; d=json.load(open('$PROJECT_DIR/pre/agent_config.json')); print(d.get('start_command',''))" 2>/dev/null)
        if [[ -n "$custom" ]]; then
            START_CMD="$custom"
        fi
    fi
    info "起 tmux session [$TMUX_SESSION] 在 $PROJECT_DIR 跑: $START_CMD (rc=$LOCAL_TMUX_RC)"
    # wrap with tmux_startup rc (proxy + JP egress 验证)
    WRAPPED_CMD="bash -ic \"source $LOCAL_TMUX_RC && exec $START_CMD\""
    tmux new-session -d -s "$TMUX_SESSION" -c "$PROJECT_DIR" "$WRAPPED_CMD"
    sleep 2
    if tmux has-session -t "=$TMUX_SESSION" 2>/dev/null; then
        ok "tmux session $TMUX_SESSION 起来了"
    else
        emph "tmux session $TMUX_SESSION 起失败, 检查启动命令: $START_CMD"
        exit 3
    fi
fi

# 4. 通知 master 重发现
info "调 master 重发现 $NODE_ID"
resp=$(curl -sS -X POST "$MASTER_URL/api/v1/nodes/$NODE_ID/rediscover" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $TOKEN" \
    -d '{}' || true)
if [[ -n "$resp" ]]; then
    ok "rediscover: $resp"
else
    warn "rediscover 没响应, master 可能不在线"
fi

# 5. 检查 agent 是否注册成功 (sleep 一下让 node 处理)
sleep 1
agent_id="$NODE_ID.cli-claude-code-local.$PROJECT"
listed=$(curl -sS -H "Authorization: Bearer $TOKEN" "$MASTER_URL/api/v1/agents" 2>/dev/null \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(any(a['agent_id']=='$agent_id' for a in d.get('agents',[])))" 2>/dev/null || echo "False")
if [[ "$listed" == "True" ]]; then
    ok "agent $agent_id 已注册到总线"
else
    warn "agent $agent_id 未在总线列表中, 可能要再 rediscover"
fi
