"""
pre Stop 分析引擎
读取 agent 最近日志 + 规则, 调用 claude -p 分析停止原因并给出下一步建议
"""
import subprocess
import json
import os
import re

try:
    from .governor import _load_file, _shell_quote
except ImportError:
    from governor import _load_file, _shell_quote  # type: ignore[no-redef]

# 从 claude -p 输出中提取结构化分析
_REASON_RE = re.compile(r"STOP_REASON:\s*(COMPLETED|EXPLORING|ERROR|BLOCKED|UNCERTAIN|IDLE)", re.IGNORECASE)
_ACTION_RE = re.compile(r"NEXT_ACTION:\s*(.+?)(?:\n|$)", re.IGNORECASE)
_EXPLANATION_RE = re.compile(r"EXPLANATION:\s*(.+?)(?:\n|$)", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"CONFIDENCE:\s*(HIGH|MEDIUM|LOW)", re.IGNORECASE)
_FINDING_LEVEL_RE = re.compile(r"FINDING_LEVEL:\s*(INFO|WARNING|CRITICAL)", re.IGNORECASE)
_FINDING_TITLE_RE = re.compile(r"FINDING_TITLE:\s*(.+?)(?:\n|$)", re.IGNORECASE)
_FINDING_CONTENT_RE = re.compile(r"FINDING_CONTENT:\s*(.+?)(?:\n|$)", re.IGNORECASE)


def analyze_stop(agent_pre_dir: str, rules_dir: str, cwd: str,
                 log_dir: str, session_id: str = "",
                 last_n: int = 20, timeout: int = 0,
                 transcript_path: str = "",
                 provider: str = "claude",
                 tmux_session: str = "",
                 tmux_lines: int = 500) -> dict:
    """
    分析 agent 停止原因, 返回结构化建议

    Returns:
        {
            "stop_reason": "COMPLETED|ERROR|BLOCKED|UNCERTAIN|IDLE",
            "next_action": "下一步指令",
            "explanation": "给用户的解释",
            "confidence": "HIGH|MEDIUM|LOW",
            "raw": "claude -p 原始输出"
        }
    """
    # --- 收集上下文 ---
    recent_logs = _get_recent_logs(log_dir, cwd, last_n)
    # system 层 (install.sh 强更): output contract + hard rules (anti-loop / no-user)
    system_rules = _load_file(os.path.join(rules_dir, "system_analyze.md")) if rules_dir else ""
    # global 层 (用户编辑): priority ladder / finding levels
    global_rules = _load_file(os.path.join(rules_dir, "global_analyze.md")) if rules_dir else ""
    # 项目规则: {cwd}/pre/analyze_rules.md (跟随项目)
    agent_rules = _load_file(os.path.join(cwd, "pre", "analyze_rules.md")) if cwd else ""
    agent_config = load_agent_config(agent_pre_dir, cwd)

    # --- tmux pane 上下文 (接近人眼看到的终端内容, 优先级最高) ---
    tmux_context = _capture_tmux_pane(tmux_session, tmux_lines) if tmux_session else ""

    # --- 对话上下文 (transcript, 作为 fallback) ---
    from .governor import _get_transcript_context
    conversation_context = _get_transcript_context(
        transcript_path, last_n=15, max_chars_per_msg=500
    ) if transcript_path and not tmux_context else ""

    # --- 最近写入/修改的文件 ---
    recent_files = _get_recent_files(cwd)

    # --- 构建 prompt ---
    parts = [
        f"[STOP ANALYSIS] Agent stopped. CWD: {cwd}",
        f"Agent mode: {agent_config.get('mode', 'supervised')}",
    ]

    if tmux_context:
        parts.append(f"\n--- TERMINAL CONTENT (what the user sees, last {tmux_lines} lines) ---\n{tmux_context}")
    elif conversation_context:
        parts.append(f"\n--- RECENT CONVERSATION (last 15 messages) ---\n{conversation_context}")

    if recent_files:
        parts.append(f"\n--- RECENTLY MODIFIED FILES ---\n{recent_files}")

    parts.extend([
        f"\n--- RECENT TOOL CALL LOGS (last {last_n}) ---",
        recent_logs if recent_logs else "(no recent logs)",
    ])

    if system_rules:
        parts.append(f"\n--- SYSTEM ANALYZE RULES ---\n{system_rules}")

    if global_rules:
        parts.append(f"\n--- GLOBAL ANALYZE RULES ---\n{global_rules}")

    if agent_rules:
        parts.append(f"\n--- AGENT-SPECIFIC ANALYZE RULES ---\n{agent_rules}")

    if not system_rules and not global_rules and not agent_rules:
        parts.append("\nAnalyze why the agent stopped. Reply with STOP_REASON, NEXT_ACTION, EXPLANATION, CONFIDENCE.")

    prompt = "\n".join(parts)

    # --- 调用 AI 分析 ---
    system_prompt = (
        "You are a stop analyzer for Claude Code agents. "
        "Your ONLY output format is exactly 4 lines:\n"
        "STOP_REASON: EXPLORING|COMPLETED|ERROR|BLOCKED|UNCERTAIN|IDLE\n"
        "NEXT_ACTION: <one concrete instruction>\n"
        "EXPLANATION: <brief explanation>\n"
        "CONFIDENCE: HIGH|MEDIUM|LOW\n"
        "Do NOT output any other text, greetings, markdown, or preamble. "
        "Just these 4 lines, nothing else."
    )

    if provider == "gemini":
        full_prompt = f"{system_prompt}\n\n{prompt}"
        cmd = f'source ~/rule.sh && GEMINI_CLI_TRUST_WORKSPACE=true gemini -p {_shell_quote(full_prompt)} -o text'
    else:
        cmd = (
            f'source ~/rule.sh && '
            f'claude -p {_shell_quote(prompt)} '
            f'--system-prompt {_shell_quote(system_prompt)}'
        )

    try:
        run_kwargs = {
            "shell": True,
            "executable": "/bin/bash",
            "capture_output": True,
            "text": True,
            "cwd": agent_pre_dir,
        }
        if timeout > 0:
            run_kwargs["timeout"] = timeout
        result = subprocess.run(cmd, **run_kwargs)

        if result.returncode != 0:
            r = _fallback_result(f"claude -p exit {result.returncode}: {result.stderr[:200]}")
            r["prompt"] = prompt
            return r

        raw = result.stdout.strip()
        r = _parse_analysis(raw)
        r["prompt"] = prompt
        return r

    except subprocess.TimeoutExpired:
        r = _fallback_result(f"analysis timeout after {timeout}s")
        r["prompt"] = prompt
        return r
    except Exception as e:
        return _fallback_result(f"analysis error: {e}")


def load_agent_config(agent_pre_dir: str, cwd: str = "") -> dict:
    """
    加载 agent 配置, 优先从项目 {cwd}/pre/agent_config.json 读取,
    fallback 到 pre/agents/{project}/agent_config.json
    默认 supervised 模式
    """
    default = {"mode": "supervised"}

    # 优先: 项目目录下的 pre/agent_config.json
    if cwd:
        project_config = os.path.join(cwd, "pre", "agent_config.json")
        try:
            if os.path.isfile(project_config):
                with open(project_config) as f:
                    default.update(json.load(f))
                return default
        except (json.JSONDecodeError, OSError):
            pass

    # fallback: pre/agents/{project}/agent_config.json
    config_path = os.path.join(agent_pre_dir, "agent_config.json")
    try:
        if os.path.isfile(config_path):
            with open(config_path) as f:
                default.update(json.load(f))
    except (json.JSONDecodeError, OSError):
        pass
    return default


def save_agent_config(agent_pre_dir: str, config: dict, cwd: str = ""):
    """保存 agent 配置, 优先写到项目 {cwd}/pre/, fallback 到 agent_pre_dir"""
    if cwd:
        pre_dir = os.path.join(cwd, "pre")
        os.makedirs(pre_dir, exist_ok=True)
        config_path = os.path.join(pre_dir, "agent_config.json")
    else:
        os.makedirs(agent_pre_dir, exist_ok=True)
        config_path = os.path.join(agent_pre_dir, "agent_config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _get_recent_logs(log_dir: str, cwd: str, last_n: int) -> str:
    """从日志目录读取该 agent (按 cwd 过滤) 的最近 N 条日志"""
    if not os.path.isdir(log_dir):
        return ""

    # 找最新的日志文件
    files = sorted(
        [f for f in os.listdir(log_dir) if f.startswith("pre_hook_") and f.endswith(".jsonl")],
    )

    entries = []
    for fname in files[-3:]:  # 最多看最近 3 天 (正序读, 保证时间顺序)
        fpath = os.path.join(log_dir, fname)
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        # 只收集工具调用日志, 过滤掉 stop event (避免自引用污染)
                        if e.get("cwd", "") == cwd and e.get("event") != "stop":
                            entries.append(e)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

    # 取最后 N 条
    recent = entries[-last_n:]

    # 格式化为可读文本
    lines = []
    for e in recent:
        ts = e.get("ts", "")[:19]
        tool = e.get("tool", "?")
        decision = e.get("decision", "?")
        source = e.get("source", "")
        reason = e.get("reason", "")

        detail = ""
        inp = e.get("input", {})
        if tool == "Bash":
            detail = inp.get("command", e.get("command_preview", ""))[:100]
        elif tool in ("Read", "Write", "Edit"):
            detail = inp.get("file_path", e.get("file_path", ""))
        elif tool in ("Grep", "Glob"):
            detail = inp.get("pattern", e.get("pattern", ""))

        line = f"  {ts} [{decision:5s}] {tool:<10s} ({source}) {detail}"
        if reason:
            line += f" | {reason[:80]}"
        lines.append(line)

    return "\n".join(lines)


def _parse_analysis(raw: str) -> dict:
    """从 claude -p 输出中提取结构化分析, 正则优先, fallback 取全文"""
    result = {
        "stop_reason": "UNCERTAIN",
        "next_action": "",
        "explanation": "",
        "confidence": "MEDIUM",
        "raw": raw[:2000],
    }

    if not raw:
        result["confidence"] = "LOW"
        return result

    m = _REASON_RE.search(raw)
    if m:
        result["stop_reason"] = m.group(1).upper()

    m = _ACTION_RE.search(raw)
    if m:
        result["next_action"] = m.group(1).strip()

    m = _EXPLANATION_RE.search(raw)
    if m:
        result["explanation"] = m.group(1).strip()

    m = _CONFIDENCE_RE.search(raw)
    if m:
        result["confidence"] = m.group(1).upper()

    # Fallback: 正则没提取到 next_action, 用原始输出的前 500 字符作为指令
    if not result["next_action"] and raw:
        result["next_action"] = raw[:500]
        result["confidence"] = "MEDIUM"

    # 提取关键发现 (可选字段)
    m = _FINDING_LEVEL_RE.search(raw)
    if m:
        result["finding_level"] = m.group(1).upper()
    m = _FINDING_TITLE_RE.search(raw)
    if m:
        result["finding_title"] = m.group(1).strip()
    m = _FINDING_CONTENT_RE.search(raw)
    if m:
        result["finding_content"] = m.group(1).strip()

    return result


def send_to_tmux(session: str, text: str, timeout: int = 5) -> bool:
    """
    通过 tmux send-keys 注入文本到指定 session, 两步发送:
    1. -l literal 模式发文本 (Ink UI 安全)
    2. 短暂 sleep 等 Ink 渲染完
    3. 单独发 Enter 键 (触发 submit 事件而非 \\r 字符)

    单条 [text, "C-m"] 在 Claude Code Ink UI 下不触发提交 (文本和 C-m
    被 Ink 当成同一批 stdin 处理, C-m 被插入为字符 U+000D 而非按键事件)。
    分两步 + 间隔解决此问题。
    """
    if not session or not text:
        return False
    try:
        import shutil
        import time
        tmux_bin = shutil.which("tmux") or "tmux"

        # 先验证 session 存在 (=name exact match 防 prefix bug)
        check = subprocess.run(
            [tmux_bin, "has-session", "-t", f"={session}"],
            capture_output=True, text=True, timeout=timeout,
        )
        if check.returncode != 0:
            return False

        # 步骤 1: 用 -l literal 发文本, 避免文本里若有 C-m / Enter 字面量被误解析
        r1 = subprocess.run(
            [tmux_bin, "send-keys", "-t", session, "-l", text],
            capture_output=True, text=True, timeout=timeout,
        )
        if r1.returncode != 0:
            return False

        # 步骤 2: 等 Ink UI 渲染完文本 (经验值 0.2s)
        time.sleep(0.2)

        # 步骤 3: 单独发 Enter 触发提交
        r2 = subprocess.run(
            [tmux_bin, "send-keys", "-t", session, "Enter"],
            capture_output=True, text=True, timeout=timeout,
        )
        return r2.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return False


def _capture_tmux_pane(session: str, lines: int = 500) -> str:
    """
    用 tmux capture-pane 抓取指定 session 当前 pane 的内容
    这是人眼能看到的完整终端视图 (Claude Code UI 渲染后的样子)
    """
    if not session:
        return ""
    try:
        # shutil.which 自动适配 macOS (/opt/homebrew/bin) 和 Linux (/usr/bin)
        import shutil
        tmux_bin = shutil.which("tmux") or "tmux"
        result = subprocess.run(
            [tmux_bin, "capture-pane",
             "-t", session, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _get_recent_files(cwd: str, max_files: int = 10) -> str:
    """列出 cwd 下最近 30 分钟内修改的文件"""
    if not cwd or not os.path.isdir(cwd):
        return ""
    import time
    cutoff = time.time() - 1800  # 30 分钟
    recent = []
    try:
        for root, dirs, files in os.walk(cwd):
            # 跳过 node_modules, .git, __pycache__ 等
            dirs[:] = [d for d in dirs if d not in (
                "node_modules", ".git", "__pycache__", ".venv", "dist", "build"
            )]
            for f in files:
                fpath = os.path.join(root, f)
                try:
                    mtime = os.path.getmtime(fpath)
                    if mtime > cutoff:
                        rel = os.path.relpath(fpath, cwd)
                        recent.append((mtime, rel))
                except OSError:
                    continue
    except OSError:
        return ""

    recent.sort(key=lambda x: -x[0])  # 最新的在前
    lines = [f"  {os.path.basename(r)} ({r})" for _, r in recent[:max_files]]
    return "\n".join(lines) if lines else ""


def _fallback_result(error_msg: str) -> dict:
    """分析失败时的降级结果"""
    return {
        "stop_reason": "UNCERTAIN",
        "next_action": "",
        "explanation": error_msg,
        "confidence": "LOW",
        "raw": error_msg,
    }
