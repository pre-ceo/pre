"""
pre 本地规则引擎
零延迟前置过滤: 安全操作直接 allow, 危险操作直接 ask, 灰区交给 governor
"""
import os
import re
import sys

# Load PRE_ROOT for safe-list path patterns
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common.paths import PRE_ROOT

# ---------- 决策常量 ----------
ALLOW = "allow"
ASK = "ask"
GOVERNOR = None       # 表示本地无法判断, 交给 governor
GOVERNOR_NO_CACHE = "governor_no_cache"  # 必须走 governor 且不缓存 (如 npm 供应链审查)


# ---------- Bash 白名单: 直接 allow 的命令前缀 ----------
_BASH_SAFE_PREFIXES = (
    "git status", "git log", "git diff", "git branch", "git show",
    "git rev-parse", "git remote",
    "git add", "git commit", "git tag", "git stash",
    "git checkout", "git switch", "git merge", "git rebase",
    "git fetch", "git pull", "git push",
    "ls", "pwd", "which", "whoami", "date", "echo", "wc",
    "head", "tail", "cat", "file", "stat", "find", "tree",
    "grep", "rg", "ag",
    "uv run python",  # 项目内 python 执行
    # 总线/agent 控制脚本 (常规部署操作)
    "bash scripts/bus_ctl.sh",
    f"bash {PRE_ROOT}/scripts/bus_ctl.sh",
    "bash scripts/spawn_agent.sh",
    f"bash {PRE_ROOT}/scripts/spawn_agent.sh",
    "bash scripts/fe_ctl.sh",
    f"bash {os.path.dirname(PRE_ROOT)}/pre_ui/scripts/fe_ctl.sh",
    # tmux 常规 (capture-pane / has-session / list-sessions / send-keys 给 agent-msg MCP 用)
    "tmux capture-pane", "tmux has-session", "tmux list-sessions",
    "tmux send-keys",
)

# ---------- Bash 黑名单: 包含这些关键词直接 ask ----------
_BASH_DANGER_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|--force|-[a-zA-Z]*f[a-zA-Z]*r)\b"),  # rm -rf 各种变体
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bgit\s+(push\s+--force|push\s+-f|reset\s+--hard|clean\s+-f)\b"),
    re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.IGNORECASE),
    re.compile(r"\bkill\s+-9\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if=\b"),
    re.compile(r"\b>\s*/dev/sd[a-z]\b"),
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh\b"),       # curl | sh
    re.compile(r"\bwget\b.*\|\s*(ba)?sh\b"),        # wget | sh
    re.compile(r"\bnc\s+-[a-z]*l\b"),               # netcat listen (reverse shell)
    re.compile(r"\bchmod\s+777\b"),
]

# ---------- Bash 内联命令安全模式 (命中即直接 allow, 不走 governor) ----------
# 这些模式必须满足"读类 + 单行 + 无 fs write/network POST/exec/sudo"
_INLINE_DANGER_KEYWORDS = re.compile(
    r"\b(rm\s+-rf|sudo|exec\s*\(|eval\s*\(|os\.system|subprocess|shell=True|"
    r"open\([^,)]+,\s*['\"][wa]|fs\.(writeFile|appendFile|unlink|rename)|"
    r"child_process|execSync|spawnSync|require\s*\(\s*['\"]child_process|"
    r"-X\s+(POST|PUT|DELETE|PATCH)|"
    r"\|\s*(ba)?sh|>\s*/dev|chmod\s+[0-7]*[2367]|"
    r"DROP\s+(TABLE|DATABASE)|TRUNCATE)",
    re.IGNORECASE,
)
# inline safe 子模式: 这些 inline 调用 + 内容明显读类 → allow
_INLINE_SAFE_RE = [
    # bash -c "echo/ls/cat/pwd/which/...": 简单 read 命令
    re.compile(r"^bash\s+-c\s+['\"]?(echo|ls|cat|pwd|which|whoami|date|head|tail|wc|stat|file|tree|grep|rg|du|df)\b"),
    re.compile(r"^sh\s+-c\s+['\"]?(echo|ls|cat|pwd|which|whoami|date|head|tail|wc|stat|file)\b"),
    # node -e "console.log(...)": 仅 console.log + JSON.parse + 字面量
    re.compile(r"^node\s+-e\s+['\"]console\.log\("),
    # python -c "print(...)/import json": 仅 print/json/sys/os.path 只读 / version 类
    re.compile(r"^python[23]?\s+-c\s+['\"](print\(|import\s+(json|sys|os|time|datetime|math)|sys\.version)"),
    # curl 本机 GET (没 -X POST/PUT, 限 127.0.0.1/localhost)
    re.compile(r"^curl\b(?:\s+-[a-zA-Z]+)*\s+(?:[\"']?https?://(127\.0\.0\.1|localhost)[:/])"),
    # master 自己 API curl 调 (agents 之间通过 master 总线 chat/dispatch_brief/cron_trigger 等)
    # POST/PUT/DELETE 都允许, 限 127.0.0.1:19500/api/v1/* 内部端点 (agent_id/send, /cron/trigger, /usage/event 等)
    re.compile(r"curl\b[\s\S]*?https?://(127\.0\.0\.1|localhost):19500/api/v1/"),
    # ssh host "ls/cat/grep/...": 远程只读
    re.compile(r"^ssh\s+(?:-[a-zA-Z]+\s+\S+\s+)?[\w.-]+\s+['\"](ls|cat|grep|head|tail|wc|find|stat|file|du|df|tail\s+-f|pm2\s+(list|status|logs)|systemctl\s+status|docker\s+ps|tmux\s+(list-sessions|capture-pane|has-session))\b"),
]


_MASTER_API_RE = re.compile(
    r"curl\b[\s\S]*?https?://(127\.0\.0\.1|localhost):19500/api/v1/",
    re.IGNORECASE,
)


def _is_inline_safe(cmd: str) -> bool:
    """内联命令是否明确安全 (绕开 governor 直接 allow).
    master 自己 API curl 调 优先放行 (绕过 -X POST danger 检查)."""
    # pre 内部 master API 信任放行 (agent ↔ master 总线通信合法)
    if _MASTER_API_RE.search(cmd):
        return True
    # 先排除任何危险关键词 (即使前缀匹配安全也不放)
    if _INLINE_DANGER_KEYWORDS.search(cmd):
        return False
    # 命中安全模式之一
    for pat in _INLINE_SAFE_RE:
        if pat.match(cmd):
            return True
    return False


# ---------- Bash 必须走 governor 且不缓存的命令 (供应链审查) ----------
_BASH_GOVERNOR_NO_CACHE = [
    # 供应链审查
    re.compile(r"\bnpm\s+(install|ci|add|update)\b"),
    re.compile(r"\bnpx\s+"),
    re.compile(r"\byarn\s+(add|install)\b"),
    re.compile(r"\bpnpm\s+(add|install)\b"),
    re.compile(r"\bpip\s+install\b"),
    re.compile(r"\buv\s+(add|pip\s+install)\b"),
    # 内联代码执行 (命令内容每次不同, 必须逐次审查)
    re.compile(r"\bnode\s+-e\b"),
    re.compile(r"\bpython[23]?\s+-c\b"),
    re.compile(r"\bruby\s+-e\b"),
    re.compile(r"\bperl\s+-e\b"),
    # sudo (项目 pre/rules.md 可覆盖, governor 根据上下文判断)
    re.compile(r"(?:^|&&|\|\||;|\|)\s*sudo\b"),
    # SSH 远程执行 (远程命令内容不可预测)
    re.compile(r"\bssh\b.*\s+['\"]"),           # ssh host "cmd"
    re.compile(r"\bssh\b.*\s+\w+@\w+\s+\w"),   # ssh user@host cmd
]


def evaluate(tool_name: str, tool_input: dict, cwd: str) -> tuple:
    """
    本地规则评估

    Returns:
        (decision, reason)
        decision: "allow" | "ask" | None
        None = 本地无法判断, 需要 governor
    """
    # 归一化工具名 (Claude Code + Gemini CLI 通用)
    if tool_name in ("Read", "Grep", "Glob", "read_file", "grep_search", "glob"):
        return _check_read_scope(tool_name, tool_input, cwd)

    if tool_name in ("Write", "Edit", "write_file", "replace"):
        return _check_write_scope(tool_name, tool_input, cwd)

    if tool_name in ("Bash", "run_shell_command"):
        return _check_bash(tool_input, cwd)

    # Agent, WebSearch, WebFetch, Skill, ToolSearch 等 → allow
    return (ALLOW, "")


def _check_read_scope(tool_name: str, tool_input: dict, cwd: str) -> tuple:
    """Read/Grep/Glob: cwd 内 → allow, 越界 → governor"""
    path = ""
    if tool_name in ("Read", "read_file"):
        # Claude: file_path | Gemini: absolute_path
        path = tool_input.get("file_path", "") or tool_input.get("absolute_path", "")
    elif tool_name in ("Grep", "Glob", "grep_search", "glob"):
        # Claude: path | Gemini: dir_path
        path = tool_input.get("path", "") or tool_input.get("dir_path", "")

    # 没有指定 path 的 Grep/Glob 默认在 cwd → allow
    if not path:
        return (ALLOW, "")

    if _is_within(path, cwd):
        return (ALLOW, "")

    # 允许读取 home 目录下的配置文件 (.claude, .gitconfig 等)
    home = os.path.expanduser("~")
    if _is_within(path, home):
        return (ALLOW, "")

    return (GOVERNOR, f"read scope escape: {path} outside {cwd}")


def _check_write_scope(tool_name: str, tool_input: dict, cwd: str) -> tuple:
    """Write/Edit: cwd 内 → allow, 越界 → governor"""
    # Claude: file_path | Gemini: file_path/absolute_path
    path = tool_input.get("file_path", "") or tool_input.get("absolute_path", "")
    if not path:
        return (GOVERNOR, "write without file_path")

    if _is_within(path, cwd):
        return (ALLOW, "")

    # home 目录下的 .claude 配置允许写入
    claude_dir = os.path.join(os.path.expanduser("~"), ".claude")
    if _is_within(path, claude_dir):
        return (ALLOW, "")

    return (GOVERNOR, f"write scope escape: {path} outside {cwd}")


def _check_bash(tool_input: dict, cwd: str) -> tuple:
    """Bash: 白名单 → allow, 黑名单 → ask, 灰区 → governor"""
    cmd = tool_input.get("command", "").strip()

    if not cmd:
        return (ALLOW, "")

    # 黑名单优先: 危险模式 (在白名单之前, 防止 git push --force 被白名单放行)
    for pattern in _BASH_DANGER_PATTERNS:
        if pattern.search(cmd):
            return (ASK, f"dangerous pattern: {pattern.pattern}")

    # ssh+sudo 非写入 allowlist 层
    # 决策链顺序: 全局黑 (上面已查) → ssh_sudo blacklist override → ssh_sudo allowlist → 后续缓存/governor
    # 仅当 cmd 含 ssh/sudo 类才进, 不影响其他命令
    s = cmd.lstrip()
    if s.startswith("ssh ") or s.startswith("sudo ") \
            or s.startswith("ssh\t") or s.startswith("sudo\t"):
        try:
            from ssh_sudo_allowlist import check_with_audit, ALLOW as _AL, DENY as _DN, GOVERNOR as _GV
            agent_id = tool_input.get("_agent_id", "?")
            host = tool_input.get("_ssh_host", "")
            decision, reason, rule = check_with_audit(cmd, agent_id, host)
            if decision == _AL:
                return (ALLOW, f"ssh_sudo_allowlist:{rule}")
            if decision == _DN:
                return (ASK, f"ssh_sudo_blacklist:{reason}")
            # GOVERNOR (fail-safe, M5/HC-PRE-2): fall through 走后续 _is_inline_safe / governor
        except Exception:
            # 模块异常 → 不影响主流程, 走后续 (HC-PRE-2)
            pass

    # 内联命令安全白名单 (绕开 governor 直接 allow)
    if _is_inline_safe(cmd):
        return (ALLOW, "inline_safe_pattern")

    # 供应链审查: 必须走 governor, 每次都分析, 不缓存
    for pattern in _BASH_GOVERNOR_NO_CACHE:
        if pattern.search(cmd):
            return (GOVERNOR_NO_CACHE, f"supply chain review: {pattern.pattern}")

    # prefix-allow 是性能层 (零延迟), 不是 LLM judgment 层.
    # 命令命中 cat/head/tail/grep 等 read prefix, 但同时含 always-sensitive 路径
    # (.ssh / 私钥 / 系统凭证) 或副作用 (pipe to external, 重定向写入) → fall through
    # 让 governor LLM 判.
    if _has_sensitive_override(cmd):
        return (GOVERNOR, "sensitive content / exfiltration vector — defer to governor")

    # 白名单: 安全命令前缀
    for prefix in _BASH_SAFE_PREFIXES:
        if cmd.startswith(prefix):
            return (ALLOW, "")

    # 动态白名单: cwd 内的 python 脚本
    if cmd.startswith(f"python3 {cwd}/") or cmd.startswith(f"python {cwd}/"):
        return (ALLOW, "")

    # 灰区: 交给 governor
    return (GOVERNOR, "")


# ---------- prefix-allow sensitive override ----------
# 只有"真正泄露即坏"的路径 (私钥 / 系统级凭证) 才永远 fall-through 到 governor.
# .env 路径不单独触发 fall-through: 纯读不算泄露, transcript 是 user 私有上下文.
# 只有配 exfiltration (pipe to external / 写非 tmp) 才转交 governor.
_ALWAYS_SENSITIVE_RE = re.compile(
    r"(?:^|[\s'\";|&<>()/`]|\.\./|~/?)"
    r"(?:"
    r"/?\.ssh/"                               # .ssh/ 或 /.ssh/ 路径
    r"|id_(?:rsa|ed25519|ecdsa|dsa)\b"        # 私钥文件名
    r"|\.aws/credentials\b"                   # AWS 凭证
    r"|\.gnupg/"                              # GPG 私钥目录
    r"|/etc/(?:shadow|passwd|sudoers)\b"      # 系统级凭证
    r"|\.config/gh/hosts\.yml\b"              # GitHub CLI token
    r")",
    re.IGNORECASE,
)
# Exfiltration: 输出 pipe 到外部网络 / 写入非 cwd 持久化.
# atomic group `(?>\s*)` 仅 Python 3.11+; 用 zero-width lookahead `(?!\s*X)` 兼容 3.10.
_EXFIL_VECTOR_RE = re.compile(
    r"\|\s*(?:curl|wget|nc|netcat|ssh|scp|rsync)\b"                  # pipe to external
    r"|(?<![>&])>>(?!\s*(?:/dev/null\b|/tmp/|/var/tmp/))"             # append (排除 &>>)
    r"|(?<![>&\d])>(?![>&])(?!\s*(?:/dev/null\b|/tmp/|/var/tmp/|/dev/stderr|/dev/stdout))"  # > redirect
    r"|\|\s*tee\b(?!\s+/dev/null)",                                  # tee non-null
)


def _has_sensitive_override(cmd: str) -> bool:
    """prefix-allow override 检查.
    返 True = fall through 到 governor, False = 继续走 prefix-allow.
    """
    if _EXFIL_VECTOR_RE.search(cmd):
        return True
    if _ALWAYS_SENSITIVE_RE.search(cmd):
        return True
    return False


def _is_within(path: str, base: str) -> bool:
    """检查 path 是否在 base 目录内 (解析符号链接和 ../)"""
    try:
        real_path = os.path.realpath(path)
        real_base = os.path.realpath(base)
        return real_path.startswith(real_base + os.sep) or real_path == real_base
    except (ValueError, OSError):
        return False
