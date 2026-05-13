"""
pre task summarizer — 用 gemini -p (复用 governor 模式) 总结 agent 当前任务.

输入: agent 工作上下文 (pane / 派单标题 / 工具调用 / claude 状态)
输出: 20 字以内中文短语, 反映"agent 现在的整体任务"

零外部依赖 (subprocess + gemini CLI).
fail-safe: 任何错误返回 None, 不阻塞调用方.
"""
from __future__ import annotations
import os
import subprocess
from typing import Optional


PROMPT_TEMPLATE = """你是一个 agent 工作总结助手. 下面是某个 claude code agent 的当前工作上下文, 请用 20 字以内中文短语总结它"现在正在做的整体任务" (不是单步动作, 而是任务目标).

要求:
- 输出仅 20 字以内一行短语, 无引号无解释无标点结尾
- 如果 agent 看上去 idle / 在等待输入 / 没明确任务, 输出 "空闲"
- 如果 agent 在等用户决策 (supervised), 短语应反映"等批准 X 操作"
- 如果 agent 在执行某具体任务, 短语应反映任务目标
- 不要重复历史派单文字, 根据"现在做什么"推断
- 如果 pane 显示任务进度报告 / 完成项, 短语反映正在收尾的事

上下文:
[最近派单标题 (历史背景, 可能已完成)]
{recent_dispatched_titles}

[claude 内部状态]
{claude_status}

[最近 5 个工具调用]
{recent_actions}

[pane 末尾 600 字]
{pane_tail}

输出 (20 字以内中文短语):
"""


def _shell_quote(s: str) -> str:
    """安全的 shell 引用, 防止命令注入"""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\t", " ")
    return s[:n]


def summarize_agent_task(
    agent_id: str,
    pane_text: str,
    recent_dispatched_titles: list[str] | None = None,
    recent_actions: list[dict] | None = None,
    claude_status: str | None = None,
    timeout: int = 30,
) -> Optional[str]:
    """
    返回 20 字以内中文短语 (LLM 生成), 失败/超时返回 None.

    pane_text: tmux capture-pane 文本
    recent_dispatched_titles: 最近派给该 agent 的 task_request/command 标题 (前 60 字截断)
    recent_actions: [{tool, summary}, ...] 最近工具调用列表
    claude_status: 例 "Crunched for 20m 16s" / "Cooked for 3m 4s"
    """
    if not pane_text and not recent_actions:
        return None  # 信息太少, 不调 LLM

    # 构建 context
    titles_str = "\n".join(f"- {t}" for t in (recent_dispatched_titles or [])[:3]) or "(无)"
    status_str = claude_status or "(无)"
    actions_lines = []
    for ra in (recent_actions or [])[:5]:
        tool = ra.get("tool", "?")
        summary = (ra.get("summary") or "")[:60]
        actions_lines.append(f"- {tool}: {summary}")
    actions_str = "\n".join(actions_lines) or "(无)"
    pane_tail = _truncate(pane_text[-2400:] if len(pane_text) > 2400 else pane_text, 2400)

    prompt = PROMPT_TEMPLATE.format(
        recent_dispatched_titles=titles_str,
        claude_status=status_str,
        recent_actions=actions_str,
        pane_tail=pane_tail,
    )

    cmd = f'source ~/rule.sh && GEMINI_CLI_TRUST_WORKSPACE=true gemini -p {_shell_quote(prompt)} -o text'
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None
        out = (result.stdout or "").strip()
        if not out:
            return None
        # 取第一行, 限 60 字 (中文 20 字内大致 60 byte)
        first_line = out.splitlines()[0].strip()
        # 去除可能的引号 / 句号
        first_line = first_line.strip('"\'`。.，,： :')
        # 中文 20 字 ≈ 60 字符 (按 unicode 字符数算 20)
        if len(first_line) > 30:
            first_line = first_line[:30]
        return first_line if first_line else None
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
