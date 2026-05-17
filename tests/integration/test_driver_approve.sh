#!/usr/bin/env bash
# tests/integration/test_driver_approve.sh
#
# 黑盒集成测试: 验证 codex/gemini driver 拿到 evaluator=allow 时, 真的会
# tmux send-keys 自动按下 approval 键 (而不只是 reported_to_user).
#
# 测试设计:
# 1. 创建一个文件 /tmp/<marker>.txt
# 2. 给两个 test agent 临时安装 pre/rules.md, 含"额外 ALLOW: mv /tmp/<marker>*"
#    — 让 governor LLM 看到明确规则, 大概率给 allow (绕开 LLM 在 workspace policy 上保守)
# 3. 清两个 agent 的 decision_cache.json — 强制重判
# 4. 在 tmux pane 注入 prompt 让 codex/gemini 跑 mv 命令
# 5. 等 ≤ 60s 检查 auto_decision_*.jsonl 是否各出现 action=approve_key_sent 命中 marker
#
# 期望: codex PASS / gemini PASS
# 失败原因可能:
#   - driver 不在跑 (master/node 没起)
#   - CLI 没弹 approval (codex 沙箱直接允许)
#   - LLM ignore rules.md 给 ask
#   - send_key 失败
#
# 用法:
#   bash tests/integration/test_driver_approve.sh
#   bash tests/integration/test_driver_approve.sh --keep-rules
#
# 兼容 macOS bash 3.2 (无 declare -A).

set -uo pipefail

KEEP_RULES=0
for arg in "$@"; do
    case "$arg" in
        --keep-rules) KEEP_RULES=1 ;;
        -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
    esac
done

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PRE_ROOT="$(dirname "$(dirname "$_SCRIPT_DIR")")"
PRE_LOG_ROOT="${PRE_LOG_DIR:-$(dirname "$_PRE_ROOT")/pre_log}"
RULE_ROOT="${PRE_RULE_ROOT:-$(dirname "$_PRE_ROOT")/pre_rule}"

if [[ -t 1 ]]; then
    G="\033[32m"; R="\033[31m"; Y="\033[33m"; D="\033[2m"; B="\033[1m"; N="\033[0m"
else
    G=""; R=""; Y=""; D=""; B=""; N=""
fi

pass() { printf "${G}✓ PASS${N} %s\n" "$1"; }
fail() { printf "${R}✗ FAIL${N} %s\n" "$1"; }
info() { printf "${D}  %s${N}\n" "$1"; }
warn() { printf "${Y}⚠${N} %s\n" "$1"; }

MARKER="pre_drv_approve_$(date +%s)"
# 使用 rm 而非 mv: codex 沙箱 (workspace-write) 直接放行 /tmp 内 mv 不弹 approval,
# 但 rm 是 destructive operation, codex 必弹 approval UI.
# gemini 任何 shell 都弹.
FIXTURE_FILE="/tmp/${MARKER}.txt"

# parallel indexed arrays (bash 3.2 兼容)
DRIVERS=(codex gemini)
AGENTS=(test_codex test_gemini)
TMUX_SESSIONS=(test_codex test_gemini)
# 备份/状态 — 用 tmp 文件存, 避免动态变量名
TMPDIR_RUN="$(mktemp -d -t pre_drv_test_XXXXXX)"

cleanup() {
    if [[ "$KEEP_RULES" == "1" ]]; then
        warn "--keep-rules: 不恢复 rules.md / cache (留作 debug, 内容在 $TMPDIR_RUN)"
    else
        for i in 0 1; do
            driver="${DRIVERS[$i]}"
            agent="${AGENTS[$i]}"
            rules_file="$HOME/cursor/$agent/pre/rules.md"
            cwd="$HOME/cursor/$agent"
            cache_dir_name="$(echo "$cwd" | sed 's|^/||; s|/|-|g')"
            cache_file="$RULE_ROOT/agents/$cache_dir_name/decision_cache.json"

            if [[ -f "$TMPDIR_RUN/${driver}.rules.bak" ]]; then
                cp "$TMPDIR_RUN/${driver}.rules.bak" "$rules_file"
            fi
            if [[ -f "$TMPDIR_RUN/${driver}.cache.bak" ]]; then
                cp "$TMPDIR_RUN/${driver}.cache.bak" "$cache_file"
            fi
        done
        info "rules.md / cache 已恢复"
        rm -rf "$TMPDIR_RUN"
    fi
    rm -f "/tmp/${MARKER}"*.txt 2>/dev/null || true
}
trap cleanup EXIT

# === 前置检查 ===
printf "${B}=== 前置检查 ===${N}\n"

command -v tmux >/dev/null 2>&1 || { fail "缺 tmux"; exit 2; }
info "tmux ok"

for i in 0 1; do
    ts="${TMUX_SESSIONS[$i]}"
    if ! tmux has-session -t "=$ts" 2>/dev/null; then
        fail "tmux session [$ts] 不存在 — 先 spawn ${DRIVERS[$i]} agent"; exit 2
    fi
    info "tmux session [$ts] ok"
done

tmux has-session -t "=pre-master" 2>/dev/null || warn "pre-master tmux 不在 — master 可能没跑"
tmux has-session -t "=pre-node" 2>/dev/null || warn "pre-node tmux 不在 — driver poll 没在跑"

# === 准备 fixture ===
printf "\n${B}=== 准备 fixture ===${N}\n"
# 给每个 driver 一个独立 fixture (avoid race: 一边 rm 后另一边找不到 src)
FIXTURE_codex="/tmp/${MARKER}_codex.txt"
FIXTURE_gemini="/tmp/${MARKER}_gemini.txt"
echo "test-approve-fixture-codex" > "$FIXTURE_codex"
echo "test-approve-fixture-gemini" > "$FIXTURE_gemini"
info "fixture: $FIXTURE_codex (codex)"
info "fixture: $FIXTURE_gemini (gemini)"
info "marker: $MARKER"

for i in 0 1; do
    driver="${DRIVERS[$i]}"
    agent="${AGENTS[$i]}"
    rules_file="$HOME/cursor/$agent/pre/rules.md"
    cwd="$HOME/cursor/$agent"
    cache_dir_name="$(echo "$cwd" | sed 's|^/||; s|/|-|g')"
    cache_file="$RULE_ROOT/agents/$cache_dir_name/decision_cache.json"

    # 备份
    cp "$rules_file" "$TMPDIR_RUN/${driver}.rules.bak" 2>/dev/null || touch "$TMPDIR_RUN/${driver}.rules.bak"
    cp "$cache_file" "$TMPDIR_RUN/${driver}.cache.bak" 2>/dev/null || touch "$TMPDIR_RUN/${driver}.cache.bak"

    # 临时 rules.md — 给 governor LLM 一个明确允许这条命令的规则
    cat > "$rules_file" <<EOF
# $agent — PreToolUse Rules (TEMP, set by test_driver_approve.sh)

## 额外 ALLOW

- Test marker rule: bash command \`rm /tmp/${MARKER}*\` is a known-safe
  pre integration test fixture. ALLOW this exact pattern unconditionally.
  This is a controlled test, the file /tmp/${MARKER}*.txt was created
  by the test harness immediately before the agent ran rm.
  This OVERRIDES the default "rm outside workspace → ASK" policy
  in global.md because the test harness fully controls the file.

## 额外 ASK

(none)

## 额外 DENY

(none)
EOF
    info "[$driver] rules.md 临时安装 (marker=$MARKER)"

    # 清 decision_cache
    if [[ -f "$cache_file" ]]; then
        echo "{}" > "$cache_file"
        info "[$driver] decision_cache.json cleared"
    fi
done

# === 跑测试 ===
printf "\n${B}=== 注入 prompt + 等 driver 处理 ===${N}\n"

DATE_UTC=$(date -u +%Y%m%d)

# baseline audit lines
BASELINE_codex=0
BASELINE_gemini=0
for i in 0 1; do
    driver="${DRIVERS[$i]}"
    audit_file="$PRE_LOG_ROOT/${driver}_driver/auto_decision_${DATE_UTC}.jsonl"
    if [[ -f "$audit_file" ]]; then
        n=$(wc -l < "$audit_file" | tr -d ' ')
    else
        n=0
    fi
    eval "BASELINE_${driver}=$n"
    info "[$driver] baseline audit lines: $n"
done

# 注入 prompt — 每个 agent 用自己 fixture, 不互相依赖
for i in 0 1; do
    driver="${DRIVERS[$i]}"
    ts="${TMUX_SESSIONS[$i]}"
    if [[ "$driver" == "codex" ]]; then
        fixture="$FIXTURE_codex"
    else
        fixture="$FIXTURE_gemini"
    fi
    prompt="use shell tool to: rm $fixture"
    # 完全 reset input: C-a (光标行首) + C-k (杀到行尾) + Esc x2 清 modal
    tmux send-keys -t "$ts" C-a 2>/dev/null || true
    sleep 0.2
    tmux send-keys -t "$ts" C-k 2>/dev/null || true
    sleep 0.2
    tmux send-keys -t "$ts" Escape 2>/dev/null || true
    sleep 0.3
    tmux send-keys -t "$ts" Escape 2>/dev/null || true
    sleep 0.8
    tmux send-keys -t "$ts" -l "$prompt"
    sleep 0.6
    tmux send-keys -t "$ts" Enter
    sleep 0.3
    info "[$driver] prompt 已发送到 tmux:$ts → $prompt"
done

# poll audit log
TIMEOUT=90
INTERVAL=5
printf "${D}  等待 driver 处理 (timeout=${TIMEOUT}s, interval=${INTERVAL}s)...${N}\n"

PASS_codex="pending"
PASS_gemini="pending"

ELAPSED=0
while [[ $ELAPSED -lt $TIMEOUT ]]; do
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
    ALL_DONE=1

    for i in 0 1; do
        driver="${DRIVERS[$i]}"
        status_var="PASS_${driver}"
        cur_status=$(eval "echo \$$status_var")
        [[ "$cur_status" == "pass" ]] && continue
        ALL_DONE=0

        audit_file="$PRE_LOG_ROOT/${driver}_driver/auto_decision_${DATE_UTC}.jsonl"
        [[ ! -f "$audit_file" ]] && continue

        baseline_var="BASELINE_${driver}"
        baseline_val=$(eval "echo \$$baseline_var")
        new_lines=$(tail -n "+$((baseline_val + 1))" "$audit_file" 2>/dev/null)
        [[ -z "$new_lines" ]] && continue

        match=$(printf '%s\n' "$new_lines" | MARKER_PY="$MARKER" python3 -c "
import json, os, sys
marker = os.environ['MARKER_PY']
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        continue
    if e.get('action') == 'approve_key_sent' and marker in str(e.get('tool_input_preview','')):
        print(json.dumps(e))
        break
")
        if [[ -n "$match" ]]; then
            eval "PASS_${driver}=pass"
            printf "${G}  [${driver}] approve_key_sent ✓ (t+${ELAPSED}s)${N}\n"
        fi
    done

    [[ $ALL_DONE -eq 1 ]] && break
done

# === 结果 ===
printf "\n${B}=== 结果 ===${N}\n"
OVERALL=0
for i in 0 1; do
    driver="${DRIVERS[$i]}"
    audit_file="$PRE_LOG_ROOT/${driver}_driver/auto_decision_${DATE_UTC}.jsonl"
    baseline_var="BASELINE_${driver}"
    baseline_val=$(eval "echo \$$baseline_var")
    new_lines=$(tail -n "+$((baseline_val + 1))" "$audit_file" 2>/dev/null || echo "")
    status_var="PASS_${driver}"
    cur_status=$(eval "echo \$$status_var")

    if [[ "$cur_status" == "pass" ]]; then
        pass "$driver driver — evaluator=allow, send-keys approve 已发"
    else
        fail "$driver driver — 未观察到 approve_key_sent (timeout=${TIMEOUT}s)"
        OVERALL=1
        if [[ -n "$new_lines" ]]; then
            printf "${D}  期间新增 audit:${N}\n"
            printf '%s\n' "$new_lines" | MARKER_PY="$MARKER" python3 -c "
import json, os, sys
marker = os.environ['MARKER_PY']
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        e = json.loads(line)
        ts_short = e.get('ts','?')[:19]
        tag = ' ← marker' if marker in str(e.get('tool_input_preview','')) else ''
        print(f\"    ts={ts_short} decision={e.get('decision')} source={e.get('source')} action={e.get('action')} input={e.get('tool_input_preview','')[:60]}{tag}\")
        if e.get('reason'):
            print(f\"        reason: {e.get('reason','')[:120]}\")
    except json.JSONDecodeError:
        pass
"
        else
            printf "${D}    (${driver}_driver/auto_decision_${DATE_UTC}.jsonl 期间没新增)${N}\n"
            printf "${D}    可能: CLI 没弹 approval / driver 没看到 / node 没在 poll${N}\n"
        fi
    fi
done

echo
if [[ $OVERALL -eq 0 ]]; then
    printf "${G}${B}全部 PASS${N}\n"
else
    printf "${R}${B}有 FAIL — 见上${N}\n"
fi

exit $OVERALL
