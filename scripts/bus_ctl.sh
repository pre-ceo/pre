#!/usr/bin/env bash
# scripts/bus_ctl.sh — 用 tmux 长驻 pre Master + Node, 提供 start/stop/status/logs/restart
#
# 用法:
# bash scripts/bus_ctl.sh start # 启动两者
# bash scripts/bus_ctl.sh start master|node # 单独启动
# bash scripts/bus_ctl.sh stop # 停止两者
# bash scripts/bus_ctl.sh restart
# bash scripts/bus_ctl.sh status
# bash scripts/bus_ctl.sh logs master # 查最近输出
# bash scripts/bus_ctl.sh logs node -f # attach 实时观看 (Ctrl+B D 退出)
# bash scripts/bus_ctl.sh attach master|node
#
# 环境变量覆盖:
# FNPRE_MASTER_ARGS — 覆盖 master 启动参数
# FNPRE_NODE_ARGS — 覆盖 node 启动参数
# FNPRE_PORT — master 端口 (默认 19500)
# PRE_SECRET_LEGACY — legacy 共享 secret (默认 fnpre, 仍兼容)
# PRE_NODE_SECRET — per-node raw secret (优先于 PRE_SECRET_LEGACY)
# NODE_ID — node 身份 id (默认 local, 远端必 export 真名 e.g. remote-node)
# NODE_TRANSPORT — ws-client (默认, 主动连 master) / ws-server (远端被动等 master connect)
# NODE_LISTEN_HOST — ws-server 模式 listen host (默认 127.0.0.1)
# NODE_LISTEN_PORT — ws-server 模式 listen port (默认 9500)
# NODE_CAPABILITIES — daemon capabilities (默认 cli-claude-code-local,cli-codex-local,cli-gemini-local)

set -euo pipefail

# PR3+5: ~/.pre/env 是单点 token 出入口 — source 进来让 PRE_*_SECRET 全部可见
if [[ -f "$HOME/.pre/env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$HOME/.pre/env"
    set +a
fi

# ~/.pre/rc 是 user init (proxy / PATH / nvm 等) — source 后 tmux 起的 python/node 子进程继承
if [[ -f "$HOME/.pre/rc" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$HOME/.pre/rc"
    set +a
fi

SESSION_MASTER="pre-master"
SESSION_NODE="pre-node"
SESSION_UI="preui-static"
SESSION_CRON="pre-cron"
PRE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRE_UI_PATH="${PRE_UI_PATH:-$(dirname "$PRE_DIR")/pre_ui}"
FNPRE_UI_URL="${FNPRE_UI_URL:-http://127.0.0.1:5174/index.html}"

PORT="${FNPRE_PORT:-19500}"
NODE_ID="${NODE_ID:-local}"
NODE_TRANSPORT="${NODE_TRANSPORT:-ws-client}"
NODE_LISTEN_HOST="${NODE_LISTEN_HOST:-127.0.0.1}"
NODE_LISTEN_PORT="${NODE_LISTEN_PORT:-9500}"
NODE_CAPABILITIES="${NODE_CAPABILITIES:-cli-claude-code-local,cli-codex-local,cli-gemini-local}"

# multi-token RBAC: master 的 token 在 master.db 内. node 启动时需要拿到 node-default
# token 的 raw 才能 ws connect. 来源优先级:
#   1. 启动 bus_ctl.sh 时已 export 的 PRE_NODE_TOKEN (推荐生产用 vault/keyring)
#   2. ~/.pre/data/initial_tokens.txt 里的 node-default 行 (首次启动 master 写)
# 优先 ~/.pre/env 的 PRE_NODE_SECRET (PR3+); 次选 legacy PRE_NODE_TOKEN; 末选 grep initial_tokens.txt
NODE_TOKEN="${PRE_NODE_SECRET:-${PRE_NODE_TOKEN:-}}"
if [[ -z "$NODE_TOKEN" ]]; then
    INIT_FILE="$HOME/.pre/data/initial_tokens.txt"
    if [[ -f "$INIT_FILE" ]]; then
        NODE_TOKEN=$(grep '^node-default=' "$INIT_FILE" | head -1 | cut -d= -f2-)
    fi
fi

DEFAULT_MASTER_ARGS="--port $PORT"
# node 启动参数: --secret 仍用 (传给 register_node frame), 但值是 node role 的 token raw
if [[ "$NODE_TRANSPORT" == "ws-server" ]]; then
    DEFAULT_NODE_ARGS="--node-id $NODE_ID --transport ws-server --listen-host $NODE_LISTEN_HOST --listen-port $NODE_LISTEN_PORT --secret ${NODE_TOKEN:-MISSING_NODE_TOKEN} --capabilities $NODE_CAPABILITIES"
else
    DEFAULT_NODE_ARGS="--node-id $NODE_ID --master ws://127.0.0.1:$PORT/node --secret ${NODE_TOKEN:-MISSING_NODE_TOKEN} --capabilities $NODE_CAPABILITIES"
fi

MASTER_ARGS="${FNPRE_MASTER_ARGS:-$DEFAULT_MASTER_ARGS}"
NODE_ARGS="${FNPRE_NODE_ARGS:-$DEFAULT_NODE_ARGS}"

# ---------- 颜色 (避免红绿, 用户红绿色弱) ----------
C_CYAN='\033[36m'
C_YELLOW='\033[33m'
C_BLUE='\033[34m'
C_MAGENTA='\033[35m'
C_DIM='\033[2m'
C_RESET='\033[0m'

info()  { printf "${C_BLUE}[info]${C_RESET} %s\n" "$*"; }
ok()    { printf "${C_CYAN}[ok]${C_RESET} %s\n" "$*"; }
warn()  { printf "${C_YELLOW}[warn]${C_RESET} %s\n" "$*"; }
emph()  { printf "${C_MAGENTA}[!]${C_RESET} %s\n" "$*"; }
dim()   { printf "${C_DIM}%s${C_RESET}\n" "$*"; }

require_tmux() {
    command -v tmux >/dev/null 2>&1 || { emph "tmux not found, please install"; exit 1; }
}

session_exists() {
    # exact match (=) 防 prefix bug (016 sister, agent-ceo audit 002)
    tmux has-session -t "=$1" 2>/dev/null
}

port_listening() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# ---------- start ----------
start_master() {
    if session_exists "$SESSION_MASTER"; then
        warn "$SESSION_MASTER already running"
        return 0
    fi
    info "starting master in tmux session [$SESSION_MASTER]"
    info "  args: $MASTER_ARGS"
    # cd 到 pre 根再启 (uv 必须在项目目录跑)
    tmux new-session -d -s "$SESSION_MASTER" -c "$PRE_DIR" \
        "uv run python scripts/start_master.py $MASTER_ARGS"
    # 等端口监听 (最多 10s)
    local i=0
    while ! port_listening "$PORT"; do
        sleep 0.5
        i=$((i+1))
        if [[ $i -ge 20 ]]; then
            warn "master port $PORT not listening after 10s, check logs"
            return 1
        fi
    done
    ok "master listening on :$PORT"
}

start_node() {
    if session_exists "$SESSION_NODE"; then
        warn "$SESSION_NODE already running"
        return 0
    fi
    if ! port_listening "$PORT"; then
        warn "master port $PORT not listening, start master first or wait"
    fi
    # 重读 NODE_TOKEN — master 首次启动后 initial_tokens.txt 才出现
    if [[ -z "$NODE_TOKEN" ]]; then
        INIT_FILE="$HOME/.pre/data/initial_tokens.txt"
        if [[ -f "$INIT_FILE" ]]; then
            NODE_TOKEN=$(grep '^node-default=' "$INIT_FILE" | head -1 | cut -d= -f2-)
        fi
    fi
    if [[ -z "$NODE_TOKEN" ]]; then
        emph "no node token — master 是否首次启动? 看 ~/.pre/data/initial_tokens.txt 或用 scripts/pre_token.py issue --role node --label node-XXX 发一个"
        return 1
    fi
    # rebuild NODE_ARGS with current token (NODE_TOKEN 可能在 NODE_ARGS 初始计算时为空)
    if [[ "$NODE_TRANSPORT" == "ws-server" ]]; then
        NODE_ARGS="${FNPRE_NODE_ARGS:---node-id $NODE_ID --transport ws-server --listen-host $NODE_LISTEN_HOST --listen-port $NODE_LISTEN_PORT --secret $NODE_TOKEN --capabilities $NODE_CAPABILITIES}"
    else
        NODE_ARGS="${FNPRE_NODE_ARGS:---node-id $NODE_ID --master ws://127.0.0.1:$PORT/node --secret $NODE_TOKEN --capabilities $NODE_CAPABILITIES}"
    fi
    info "starting node in tmux session [$SESSION_NODE]"
    info "  args: $(echo "$NODE_ARGS" | sed "s/$NODE_TOKEN/<NODE_TOKEN>/g")"
    tmux new-session -d -s "$SESSION_NODE" -c "$PRE_DIR" \
        "uv run python scripts/start_node.py $NODE_ARGS"
    sleep 1
    if session_exists "$SESSION_NODE"; then
        ok "node session started"
    else
        warn "node session died immediately, check logs"
        return 1
    fi
}

start_ui() {
    if ! [[ -f "$PRE_UI_PATH/scripts/fe_ctl.sh" ]]; then
        warn "fe_ctl.sh not found at $PRE_UI_PATH/scripts/, 跳过 ui (set PRE_UI_PATH 覆盖)"
        return 0
    fi
    info "starting ui via $PRE_UI_PATH/scripts/fe_ctl.sh"
    bash "$PRE_UI_PATH/scripts/fe_ctl.sh" start 2>&1 | sed 's/^/  /'
    if tmux has-session -t "=$SESSION_UI" 2>/dev/null; then
        ok "ui ready: $FNPRE_UI_URL"
    else
        warn "ui session $SESSION_UI not alive, check $PRE_UI_PATH"
    fi
}

start_cron() {
    if session_exists "$SESSION_CRON"; then
        warn "$SESSION_CRON already running"
        return 0
    fi
    info "starting cron daemon in tmux session [$SESSION_CRON]"
    tmux new-session -d -s "$SESSION_CRON" -c "$PRE_DIR" \
        "uv run python scripts/cron_daemon.py"
    sleep 1
    if session_exists "$SESSION_CRON"; then
        ok "cron daemon started (healthz http://127.0.0.1:19501/)"
    else
        warn "cron daemon died immediately, check logs"
        return 1
    fi
}

cmd_start() {
    require_tmux
    case "${1:-all}" in
        master) start_master ;;
        node)   start_node ;;
        ui)     start_ui ;;
        cron)   start_cron ;;
        all)    start_master && sleep 1 && start_node && sleep 1 && start_ui && sleep 1 && start_cron ;;
        *)      emph "unknown target: $1"; exit 1 ;;
    esac
}

# ---------- stop ----------
stop_session() {
    local s="$1"
    if session_exists "$s"; then
        info "stopping $s"
        # SIGHUP 给 session 内进程, python 应能 catch KeyboardInterrupt
        tmux send-keys -t "$s" C-c 2>/dev/null || true
        sleep 1
        tmux kill-session -t "$s" 2>/dev/null || true
        ok "$s stopped"
    else
        dim "$s not running"
    fi
}

stop_ui() {
    if [[ -f "$PRE_UI_PATH/scripts/fe_ctl.sh" ]] && tmux has-session -t "=$SESSION_UI" 2>/dev/null; then
        info "stopping ui via fe_ctl.sh"
        bash "$PRE_UI_PATH/scripts/fe_ctl.sh" stop 2>&1 | sed 's/^/  /'
    else
        dim "ui $SESSION_UI not running"
    fi
}

cmd_stop() {
    case "${1:-all}" in
        master) stop_session "$SESSION_MASTER" ;;
        node)   stop_session "$SESSION_NODE" ;;
        ui)     stop_ui ;;
        cron)   stop_session "$SESSION_CRON" ;;
        all)    stop_session "$SESSION_CRON"; stop_ui; stop_session "$SESSION_NODE"; stop_session "$SESSION_MASTER" ;;
        *)      emph "unknown target: $1"; exit 1 ;;
    esac
}

# ---------- restart ----------
cmd_restart() {
    cmd_stop "${1:-all}"
    sleep 1
    cmd_start "${1:-all}"
}

# ---------- status ----------
cmd_status() {
    require_tmux
    printf "${C_MAGENTA}━━━ pre bus status ━━━${C_RESET}\n"
    if session_exists "$SESSION_MASTER"; then
        ok "master session: $SESSION_MASTER (alive)"
    else
        dim "master session: $SESSION_MASTER (down)"
    fi
    if port_listening "$PORT"; then
        ok "master port: :$PORT listening"
    else
        dim "master port: :$PORT not listening"
    fi
    if session_exists "$SESSION_NODE"; then
        ok "node session: $SESSION_NODE (alive)"
    else
        dim "node session: $SESSION_NODE (down)"
    fi
    if session_exists "$SESSION_UI"; then
        ok "ui session: $SESSION_UI (alive)  → $FNPRE_UI_URL"
    else
        dim "ui session: $SESSION_UI (down)"
    fi
    if session_exists "$SESSION_CRON"; then
        ok "cron session: $SESSION_CRON (alive)  → http://127.0.0.1:19501/"
    else
        dim "cron session: $SESSION_CRON (down)"
    fi

    # 如果 master 跑着, 显示 nodes / agents 数量
    if port_listening "$PORT"; then
        local nodes_count agents_count
        # PR5: 修 $SECRET undefined bug — 用 PRE_HOOK_SECRET (loopback + scope 够查 nodes/agents)
        nodes_count=$(curl -sS -m 2 -H "Authorization: Bearer ${PRE_HOOK_SECRET:-MISSING_HOOK_TOKEN}" "http://127.0.0.1:$PORT/api/v1/nodes" 2>/dev/null \
            | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('nodes',[])))" 2>/dev/null || echo "?")
        agents_count=$(curl -sS -m 2 -H "Authorization: Bearer ${PRE_HOOK_SECRET:-MISSING_HOOK_TOKEN}" "http://127.0.0.1:$PORT/api/v1/agents" 2>/dev/null \
            | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('agents',[])))" 2>/dev/null || echo "?")
        info "master api: $nodes_count nodes, $agents_count agents"
    fi
}

# ---------- logs ----------
cmd_logs() {
    require_tmux
    local target="${1:-}"
    local follow="${2:-}"
    local s
    case "$target" in
        master) s="$SESSION_MASTER" ;;
        node)   s="$SESSION_NODE" ;;
        cron)   s="$SESSION_CRON" ;;
        *) emph "usage: bus_ctl.sh logs <master|node|cron> [-f]"; exit 1 ;;
    esac
    if ! session_exists "$s"; then
        warn "$s not running"
        return 1
    fi
    if [[ "$follow" == "-f" ]]; then
        info "attaching to $s (Ctrl+B then D to detach)"
        tmux attach-session -t "$s"
    else
        # 显示最近 200 行
        tmux capture-pane -t "$s" -p -S -200
    fi
}

# ---------- attach ----------
cmd_attach() {
    require_tmux
    local target="${1:-}"
    local s
    case "$target" in
        master) s="$SESSION_MASTER" ;;
        node)   s="$SESSION_NODE" ;;
        cron)   s="$SESSION_CRON" ;;
        *) emph "usage: bus_ctl.sh attach <master|node|cron>"; exit 1 ;;
    esac
    if ! session_exists "$s"; then
        warn "$s not running, start it first"
        return 1
    fi
    info "attaching to $s (Ctrl+B then D to detach without stopping)"
    tmux attach-session -t "$s"
}

# ---------- usage ----------
usage() {
    cat <<EOF
pre bus_ctl.sh — tmux 监管 Master + Node

用法:
  bash $0 start [master|node|ui|all]   启动 (默认 all: master+node+ui)
  bash $0 stop  [master|node|ui|all]   停止
  bash $0 restart [master|node|ui|all]
  bash $0 status                       查看运行状态 (含 UI URL)
  bash $0 logs <master|node> [-f]      查日志, -f 实时观看
  bash $0 attach <master|node>         进入 session 交互

环境变量:
  FNPRE_PORT          master 端口 (默认 19500)
  PRE_SECRET_LEGACY        legacy 共享 secret (默认 fnpre, 兼容)
  PRE_NODE_SECRET  per-node raw secret (优先于 PRE_SECRET_LEGACY)
  NODE_ID             node 身份 (默认 local, 远端 export 真名 e.g. remote-node)
  NODE_TRANSPORT      ws-client (默认) / ws-server (远端被动等)
  NODE_LISTEN_HOST    ws-server 模式 listen host (默认 127.0.0.1)
  NODE_LISTEN_PORT    ws-server 模式 listen port (默认 9500)
  NODE_CAPABILITIES   daemon capabilities (默认 cli-claude-code-local,cli-codex-local,cli-gemini-local)
  FNPRE_MASTER_ARGS   覆盖 master 启动参数
  FNPRE_NODE_ARGS     覆盖 node 启动参数
  PRE_UI_PATH       pre_ui 项目路径 (默认 pre 仓库的 sibling pre_ui)
  FNPRE_UI_URL        UI 入口 URL (默认 http://127.0.0.1:5174/index.html, 用户自己开浏览器)

示例 — 远端 remote-node 接入 (后): 走 master-connect 不需 NODE_ARGS 配置
  - 配置 pre_rule/remote_nodes.json 启用 remote-node (enabled: true)
  - bash $0 start  (master 启动时 RemoteNodeManager 自动 ssh exec 远端 daemon + ssh -L tunnel)
  - 远端 daemon 加载 cli-claude-code-local driver (远端 pre_rule/agents/ 自管)
  cli-claude-code-remote driver 在 删除, 不再支持本机 ssh subprocess 控远端模式.

示例 — 远端 remote-node 排查时手动起 ws-server daemon ():
  ssh remote-node-root 'set -a && source /etc/pre/env && set +a && \
    cd /root/workspace/pre && bash scripts/bus_ctl.sh restart node'
  (远端 /etc/pre/env 需 export NODE_ID=remote-node NODE_TRANSPORT=ws-server NODE_LISTEN_PORT=9500
   PRE_NODE_SECRET=...)
EOF
}

# ---------- 入口 ----------
cmd="${1:-}"
shift || true
case "$cmd" in
    start)   cmd_start "$@" ;;
    stop)    cmd_stop "$@" ;;
    restart) cmd_restart "$@" ;;
    status)  cmd_status ;;
    logs)    cmd_logs "$@" ;;
    attach)  cmd_attach "$@" ;;
    sync)
        # Phase A — multi-node sync outbound
        # usage: bus_ctl.sh sync remote-node [--dry-run] [--audit] [--force]
        # usage: bus_ctl.sh sync --all
        SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
        REPO_DIR="$( dirname "$SCRIPT_DIR" )"
        cd "$REPO_DIR"
        if [ -z "${1:-}" ]; then
            emph "usage: bus_ctl.sh sync <node|--all> [--dry-run] [--audit] [--force]"
            exit 1
        fi
        if [ "$1" = "--all" ]; then
            exec uv run python scripts/sync_to_node.py --all "${@:2}"
        else
            exec uv run python scripts/sync_to_node.py --node "$@"
        fi
        ;;
    -h|--help|help|"") usage ;;
    *)       emph "unknown command: $cmd"; usage; exit 1 ;;
esac
