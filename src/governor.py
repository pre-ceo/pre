"""
pre 治理决策模块
通过 claude -p --continue (headless 模式) 查询治理 agent 获取决策
零外部依赖 (subprocess 是 stdlib)
"""
import subprocess
import json
import os
import re

# 决策关键词正则: 从 claude -p 输出中提取 ALLOW / ASK / DENY
_DECISION_RE = re.compile(r"\b(ALLOW|ASK|DENY)\b", re.IGNORECASE)


def query_governor(tool_name: str, tool_input: dict, session_id: str,
                   cwd: str, agent_pre_dir: str, rules_dir: str = "",
                   timeout: int = 60, transcript_path: str = "",
                   provider: str = "claude") -> tuple:
    """
    调用 claude -p --continue 查询治理 agent

    Args:
        tool_name: 工具名称
        tool_input: 工具参数
        session_id: 被监控 agent 的 session_id
        cwd: 被监控 agent 的工作目录
        agent_pre_dir: 该 agent 的独立 pre 目录 (存放治理上下文)
        rules_dir: 全局规则目录路径
        timeout: claude -p 超时秒数
        transcript_path: 对话历史 JSONL 文件路径 (可选)

    Returns:
        (decision, reason) — decision: "allow" | "ask" | "deny"
    """
    # --- 加载分层规则 ---
    # system 层 (install.sh 强更, 不建议改): output contract + 绝对安全底线
    system_rules = _load_file(os.path.join(rules_dir, "system.md")) if rules_dir else ""
    # global 层 (用户编辑): operator/machine 级策略
    global_rules = _load_file(os.path.join(rules_dir, "global.md")) if rules_dir else ""
    # 项目规则: {cwd}/pre/rules.md (跟随项目, 不在 pre 目录下)
    agent_rules = _load_file(os.path.join(cwd, "pre", "rules.md")) if cwd else ""

    # --- 加载对话上下文 (最近几条消息, 帮助理解工具调用意图) ---
    context = _get_transcript_context(transcript_path) if transcript_path else ""

    # --- 构建 prompt ---
    input_summary = _summarize_input(tool_name, tool_input)

    parts = [
        f"[GOVERNANCE] Tool: {tool_name} | Session: {session_id[:12]} | CWD: {cwd}",
        f"Input: {input_summary}",
    ]

    if context:
        parts.append(f"\n--- RECENT CONVERSATION CONTEXT ---\n{context}")

    if system_rules:
        parts.append(f"\n--- SYSTEM RULES ---\n{system_rules}")

    if global_rules:
        parts.append(f"\n--- GLOBAL RULES ---\n{global_rules}")

    if agent_rules:
        parts.append(f"\n--- AGENT-SPECIFIC RULES ---\n{agent_rules}")

    if not system_rules and not global_rules and not agent_rules:
        # 无规则文件时的最小化 fallback
        parts.append("\nDefault policy: ALLOW everything. Only ASK for obviously dangerous operations.")

    parts.append(
        "\nCRITICAL FORMAT REQUIREMENT — your entire response must be:\n"
        "Line 1: exactly one word, either ALLOW or ASK (nothing else)\n"
        "Line 2: brief reason (required only for ASK, optional for ALLOW)\n"
        "Do NOT output any greeting, preamble, or other text before ALLOW/ASK."
    )

    prompt = "\n".join(parts)

    # --- 构建 shell 命令 ---
    system_prompt = (
        "You are a security gate. Your ONLY job is to evaluate tool calls. "
        "Reply with EXACTLY one word on the first line: ALLOW or ASK. "
        "If ASK, add a brief reason on the second line. "
        "Do NOT output any other text, greetings, or preamble."
    )

    if provider == "gemini":
        # gemini -p: system prompt 放在 prompt 里 (无 --system-prompt 参数)
        full_prompt = f"{system_prompt}\n\n{prompt}"
        cmd = f'source ~/rule.sh && GEMINI_CLI_TRUST_WORKSPACE=true gemini -p {_shell_quote(full_prompt)} -o text'
    elif provider == "codex":
        # codex exec, system prompt 合并 (无独立 --system-prompt 参数)
        # codex 默认 model gpt-5.5, OpenAI quota 独立于 gemini/claude
        # --skip-git-repo-check: agent_pre_dir (例 pre_rule/agents/<id>/) 不是 git repo, 加这个 flag 让 codex 不拒绝
        full_prompt = f"{system_prompt}\n\n{prompt}"
        cmd = f'source ~/rule.sh && codex exec --skip-git-repo-check {_shell_quote(full_prompt)}'
    else:
        # claude -p 需要代理 + system-prompt 覆盖 CLAUDE.md
        cmd = (
            f'source ~/rule.sh && '
            f'claude -p {_shell_quote(prompt)} --continue '
            f'--system-prompt {_shell_quote(system_prompt)}'
        )

    try:
        run_kwargs = {
            "shell": True,
            "executable": "/bin/bash",
            "capture_output": True,
            "text": True,
            "cwd": agent_pre_dir,  # 在 agent 的 pre 目录下运行, 隔离会话
        }
        if timeout > 0:
            run_kwargs["timeout"] = timeout
        result = subprocess.run(cmd, **run_kwargs)

        if result.returncode != 0:
            stderr = result.stderr.strip()[:200]
            return ("ask", f"governor exit {result.returncode}: {stderr}")

        output = result.stdout.strip()
        return _parse_decision(output)

    except subprocess.TimeoutExpired:
        return ("ask", f"governor timeout after {timeout}s")
    except Exception as e:
        return ("ask", f"governor error: {e}")


def ensure_agent_dir(pre_base_dir: str, cwd: str) -> str:
    """
    确保 agent 的独立 pre 目录存在, 返回绝对路径

    按 cwd (项目路径) 隔离, 将路径中的 / 替换为 -
    例: /home/user/projects/trading-bot → home-user-projects-trading-bot
    """
    # 去头尾 /, 替换 / 为 -, 得到目录名
    dir_name = cwd.strip("/").replace("/", "-")
    agent_dir = os.path.join(pre_base_dir, dir_name)
    os.makedirs(agent_dir, exist_ok=True)
    return agent_dir


def _summarize_input(tool_name: str, tool_input: dict) -> str:
    """精简 tool_input 用于 prompt, 避免超长"""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"command={cmd[:300]}"
    elif tool_name in ("Read", "Write", "Edit"):
        return f"file_path={tool_input.get('file_path', '')}"
    elif tool_name == "Grep":
        return f"pattern={tool_input.get('pattern', '')} path={tool_input.get('path', '')}"
    elif tool_name == "Glob":
        return f"pattern={tool_input.get('pattern', '')} path={tool_input.get('path', '')}"
    elif tool_name == "Agent":
        return f"description={tool_input.get('description', '')} prompt={tool_input.get('prompt', '')[:200]}"
    else:
        # 通用: JSON 截断
        s = json.dumps(tool_input, ensure_ascii=False)
        return s[:500] if len(s) > 500 else s


def _parse_decision(output: str) -> tuple:
    """
    从 claude -p 输出中提取决策

    预期格式:
        ALLOW
        reason text...
    或:
        ASK
        需要用户确认这个操作
    """
    if not output:
        return ("ask", "governor returned empty response")

    match = _DECISION_RE.search(output)
    if not match:
        return ("ask", f"governor no decision keyword found: {output[:200]}")

    decision = match.group(1).lower()

    # 提取 reason: 决策关键词之后的文本
    rest = output[match.end():].strip()
    # 如果 reason 在下一行
    if not rest:
        lines = output.split("\n", 2)
        rest = lines[1].strip() if len(lines) > 1 else ""

    return (decision, rest[:500])


def _get_transcript_context(transcript_path: str, last_n: int = 5,
                            max_chars_per_msg: int = 200) -> str:
    """
    从 transcript JSONL 文件读取最近 N 条消息作为上下文摘要
    只提取 user/assistant 文本消息, 跳过工具调用细节
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""

    try:
        messages = []
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    role = entry.get("role", "")
                    if role in ("user", "assistant"):
                        content = entry.get("content", "")
                        if isinstance(content, str) and content.strip():
                            messages.append(f"[{role}]: {content[:max_chars_per_msg]}")
                        elif isinstance(content, list):
                            # content 可能是 list of blocks
                            texts = []
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    texts.append(block.get("text", ""))
                            full = " ".join(texts)
                            if full.strip():
                                messages.append(f"[{role}]: {full[:max_chars_per_msg]}")
                except json.JSONDecodeError:
                    continue

        recent = messages[-last_n:]
        return "\n".join(recent) if recent else ""

    except OSError:
        return ""


def _load_file(path: str) -> str:
    """读取规则文件, 不存在时静默返回空字符��"""
    try:
        if os.path.isfile(path):
            with open(path) as f:
                content = f.read().strip()
            # 限制长度, 避免 prompt 爆炸
            return content[:3000] if len(content) > 3000 else content
    except OSError:
        pass
    return ""


def _shell_quote(s: str) -> str:
    """安全的 shell 引用, 防止命令注入"""
    # 使用单引号包裹, 内部单引号转义
    return "'" + s.replace("'", "'\"'\"'") + "'"
