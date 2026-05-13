#!/usr/bin/env bash
# scripts/install.sh — pre 一站式安装 / 重装 (idempotent).
#
# 做什么:
# - 探测 PRE_ROOT (本脚本位置), PRE_RULE_ROOT, PRE_LOG_DIR (多 fallback)
# - 写 ~/.pre/env (single source of truth; preserve token 段)
# - 从 templates/pre_rule/ 创建/同步 $PRE_RULE_ROOT (system 强更, global 保留)
# - 自动 clone pre_ui sibling (推断 url 从 pre origin remote)
# - 在 ~/.claude.json 注册 mcpServers.pre (幂等, 保留 user env keys)
# - 装 shim 到 ~/.local/bin (引用 $PRE_ROOT, mv pre 仓库后只需重跑本脚本)
# - 提议把 PATH export 加到 user shell rc (~/.zshrc 等), prompt 同意
#
# 用法:
#   bash scripts/install.sh                                  # 默认: 自动探测 + interactive prompt
#   bash scripts/install.sh --rule-root=/abs --log-dir=/abs  # 显式
#   bash scripts/install.sh -y                               # 跳过 prompt, 默认同意写 rc
#   bash scripts/install.sh --no-pre-ui                      # 跳过 pre_ui clone
#   bash scripts/install.sh --pre-ui-url=URL                 # 显式 pre_ui url
#   bash scripts/install.sh --no-mcp                         # 跳过 ~/.claude.json mcp 注册
#
# 探测优先级 (每个路径独立):
#   1. --rule-root= / --log-dir= flag
#   2. shell 当前 export 的 $PRE_RULE_ROOT / $PRE_LOG_DIR
#   3. ~/.pre/env 里上次安装的值
#   4. sibling fallback: <PRE_PARENT>/pre_rule, <PRE_PARENT>/pre_log (目录存在才用)
#   5. PRE_RULE_ROOT 不行 → 用 sibling 路径默认 (会被 install_pre_rule 自动创建);
#      PRE_LOG_DIR 不行 → 默认 <PRE_RULE_ROOT>/logs

set -euo pipefail

# === Args ===
ARG_RULE_ROOT=""
ARG_LOG_DIR=""
ARG_BIN_DIR="$HOME/.local/bin"
ARG_YES=0
ARG_NO_PRE_UI=0
ARG_PRE_UI_URL=""
ARG_NO_MCP=0
for arg in "$@"; do
    case "$arg" in
        --rule-root=*)  ARG_RULE_ROOT="${arg#*=}" ;;
        --log-dir=*)    ARG_LOG_DIR="${arg#*=}" ;;
        --bin-dir=*)    ARG_BIN_DIR="${arg#*=}" ;;
        --pre-ui-url=*) ARG_PRE_UI_URL="${arg#*=}" ;;
        --no-pre-ui)    ARG_NO_PRE_UI=1 ;;
        --no-mcp)       ARG_NO_MCP=1 ;;
        -y|--yes)       ARG_YES=1 ;;
        -h|--help)      sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# === PRE_ROOT (from script location) ===
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRE_ROOT="$(dirname "$SCRIPT_DIR")"
PRE_PARENT="$(dirname "$PRE_ROOT")"

# === ~/.pre data dir ===
PRE_DATA="$HOME/.pre"
mkdir -p "$PRE_DATA"
chmod 700 "$PRE_DATA"
ENV_FILE="$PRE_DATA/env"

# === Preserve existing env: 备份 + 读上次 path 值 + preserve 一切非 path 内容 ===
EXIST_RULE=""
EXIST_LOG=""
EXIST_OTHER=""
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d_%H%M%S)"
    EXIST_RULE=$(grep -E '^PRE_RULE_ROOT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
    EXIST_LOG=$(grep -E '^PRE_LOG_DIR=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)
    # 排除式: 保留除我们管的 path 3 行外所有内容
    # (含 token / user 自加 comment / 自定义 env / legacy PRE_SECRET 等)
    EXIST_OTHER=$(grep -vE '^(PRE_ROOT|PRE_RULE_ROOT|PRE_LOG_DIR)=' "$ENV_FILE" 2>/dev/null || true)
fi

# === PRE_RULE_ROOT resolve ===
SRC_RULE=""
if [ -n "$ARG_RULE_ROOT" ]; then
    PRE_RULE_ROOT="$ARG_RULE_ROOT"
    SRC_RULE="--rule-root flag"
elif [ -n "${PRE_RULE_ROOT:-}" ]; then
    SRC_RULE="\$PRE_RULE_ROOT shell env"
elif [ -n "$EXIST_RULE" ]; then
    PRE_RULE_ROOT="$EXIST_RULE"
    SRC_RULE="~/.pre/env (previous install)"
elif [ -d "$PRE_PARENT/pre_rule" ]; then
    PRE_RULE_ROOT="$PRE_PARENT/pre_rule"
    SRC_RULE="sibling fallback"
else
    PRE_RULE_ROOT="$PRE_PARENT/pre_rule"
    SRC_RULE="sibling default (will be created)"
fi

# === PRE_LOG_DIR resolve ===
SRC_LOG=""
if [ -n "$ARG_LOG_DIR" ]; then
    PRE_LOG_DIR="$ARG_LOG_DIR"
    SRC_LOG="--log-dir flag"
elif [ -n "${PRE_LOG_DIR:-}" ]; then
    SRC_LOG="\$PRE_LOG_DIR shell env"
elif [ -n "$EXIST_LOG" ]; then
    PRE_LOG_DIR="$EXIST_LOG"
    SRC_LOG="~/.pre/env (previous install)"
elif [ -d "$PRE_PARENT/pre_log" ]; then
    PRE_LOG_DIR="$PRE_PARENT/pre_log"
    SRC_LOG="sibling fallback"
else
    PRE_LOG_DIR="$PRE_RULE_ROOT/logs"
    SRC_LOG="default (<PRE_RULE_ROOT>/logs)"
fi

# === 写 ~/.pre/env (path 段头部 + preserve 一切现有 token / 注释 / 自定义) ===
{
    echo "# pre paths (managed by scripts/install.sh — do not hand-edit)"
    echo "PRE_ROOT=$PRE_ROOT"
    echo "PRE_RULE_ROOT=$PRE_RULE_ROOT"
    echo "PRE_LOG_DIR=$PRE_LOG_DIR"
    if [ -n "$EXIST_OTHER" ]; then
        echo ""
        echo "$EXIST_OTHER"
    fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

# === pre_rule 内容初始化 (system 强更, global 保留) ===
echo
echo "─── pre_rule sync ───"
python3 "$PRE_ROOT/scripts/install_pre_rule.py" "$PRE_RULE_ROOT" \
    || { echo "FATAL: install_pre_rule.py failed" >&2; exit 1; }

# === pre_ui sibling clone ===
# 官方上游固定 URL (开源公开维护, 例外允许 hardcode owner — pre-ceo 是项目官方账号).
PRE_UI_DEFAULT_URL="https://github.com/pre-ceo/pre_ui.git"
PRE_UI_DIR="$PRE_PARENT/pre_ui"
PRE_UI_STATUS="skipped"
if [ "$ARG_NO_PRE_UI" -eq 1 ]; then
    PRE_UI_STATUS="skipped (--no-pre-ui)"
elif [ -d "$PRE_UI_DIR" ]; then
    PRE_UI_STATUS="kept (already exists at $PRE_UI_DIR)"
else
    PRE_UI_URL="${ARG_PRE_UI_URL:-$PRE_UI_DEFAULT_URL}"
    echo
    echo "─── pre_ui clone ($PRE_UI_URL) ───"
    if git clone "$PRE_UI_URL" "$PRE_UI_DIR" 2>&1; then
        PRE_UI_STATUS="cloned to $PRE_UI_DIR"
    else
        PRE_UI_STATUS="clone failed (network?); rerun later or --pre-ui-url=URL"
        # 清理可能的部分目录
        [ -d "$PRE_UI_DIR" ] && [ ! -d "$PRE_UI_DIR/.git" ] && rm -rf "$PRE_UI_DIR"
    fi
fi

# === ~/.claude.json MCP 注册 ===
MCP_STATUS="skipped"
if [ "$ARG_NO_MCP" -eq 1 ]; then
    MCP_STATUS="skipped (--no-mcp)"
else
    echo
    echo "─── MCP registration ($HOME/.claude.json) ───"
    if python3 "$PRE_ROOT/scripts/install_mcp_registration.py" --pre-root "$PRE_ROOT"; then
        MCP_STATUS="registered mcpServers.pre"
    else
        MCP_STATUS="failed (see error above; you can add mcpServers.pre by hand)"
    fi
fi

# === 装 shim ===
mkdir -p "$ARG_BIN_DIR"
for entry in "pre:pre" "pre-tool-use:pre_tool_use.py" "pre-stop-hook:stop_hook.py"; do
    name="${entry%:*}"
    script="${entry##*:}"
    shim_path="$ARG_BIN_DIR/$name"
    cat > "$shim_path" <<SHIM
#!/usr/bin/env bash
# pre shim — installed by scripts/install.sh.
# Source ~/.pre/env for PRE_ROOT. mv pre 仓库后重跑 install.sh, shim 自动 follow.
. "\$HOME/.pre/env"
exec python3 "\$PRE_ROOT/scripts/$script" "\$@"
SHIM
    chmod 755 "$shim_path"
done

# === PATH check ===
case ":$PATH:" in
    *":$ARG_BIN_DIR:"*) PATH_OK=1 ;;
    *) PATH_OK=0 ;;
esac

# === 报告 ===
cat <<EOF

✓ pre installed
  PRE_ROOT      = $PRE_ROOT
  PRE_RULE_ROOT = $PRE_RULE_ROOT  (from $SRC_RULE)
  PRE_LOG_DIR   = $PRE_LOG_DIR  (from $SRC_LOG)
  env file      = $ENV_FILE  (chmod 600)
  shim dir      = $ARG_BIN_DIR  (pre, pre-tool-use, pre-stop-hook)
  pre_ui        = $PRE_UI_STATUS
  MCP           = $MCP_STATUS

next steps:
  1. pre bus start          # 起 master + node + ui + cron (tmux 长驻)
  2. pre init <proj>        # 给项目装 hook (PreToolUse + Stop)
  3. pre bus status         # 查 daemon 健康
EOF

# === PATH handling ===
if [ "$PATH_OK" -eq 1 ]; then
    echo "  PATH          = OK ($ARG_BIN_DIR in PATH)"
    exit 0
fi

echo
echo "⚠ $ARG_BIN_DIR is not in PATH."

# Detect shell rc
RC_FILE=""
case "$(basename "${SHELL:-}")" in
    zsh)  RC_FILE="$HOME/.zshrc" ;;
    bash) [ -f "$HOME/.bashrc" ] && RC_FILE="$HOME/.bashrc" || RC_FILE="$HOME/.bash_profile" ;;
    *)
        cat <<EOF >&2

  Unsupported shell: ${SHELL:-unknown}. Manually add this line to your shell rc:
      export PATH="$ARG_BIN_DIR:\$PATH"
EOF
        exit 0
        ;;
esac

# Idempotent check: rc already mentions .local/bin?
if [ -f "$RC_FILE" ] && grep -qE '\.local/bin' "$RC_FILE"; then
    echo "  $RC_FILE already mentions .local/bin — verify it 's in PATH, or restart shell"
    exit 0
fi

PATH_LINE="export PATH=\"$ARG_BIN_DIR:\$PATH\""

if [ "$ARG_YES" -eq 1 ]; then
    REPLY="y"
else
    echo
    printf "Append \`%s\` to %s? [y/N] " "$PATH_LINE" "$RC_FILE"
    read -r REPLY
fi

case "$REPLY" in
    y|Y|yes|YES)
        {
            echo ""
            echo "# added by pre/scripts/install.sh"
            echo "$PATH_LINE"
        } >> "$RC_FILE"
        echo "  ✓ appended to $RC_FILE"
        echo "    restart your shell or run: source $RC_FILE"
        ;;
    *)
        echo "  skipped. Manually add: $PATH_LINE"
        ;;
esac
