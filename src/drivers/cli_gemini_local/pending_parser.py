"""Gemini approval 文本解析 — 保守, tail-only, fail-closed.

实测 marker (gemini-cli v0.42, default approval-mode):
  - 强 approval marker: "Allow execution of [<tool>]?"
  - 选项行: "● 1. Allow once" / "  2. Allow for this session" /
            "  3. No, suggest changes (esc)"
  - 命令展示框: "? Shell  <command>" + box drawing 包裹命令
  - approve_key: "1", reject_key: "Escape"

跟 codex parser 同设计: fixture-driven + stale 检测, 解不出 cmd/path 时返
GeminiPending generic, 让 evaluator 走 governor/ask 而不是自动 allow.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


# 强 marker: gemini 真正在等用户确认时, 顶部 box 含这行
STRONG_APPROVAL_MARKERS = (
    "allow execution of",
    "allow execution",
)
# 弱 marker (配合 option marker 才算 active)
APPROVAL_MARKERS = STRONG_APPROVAL_MARKERS + (
    "allow once",
    "allow for this session",
    "no, suggest changes",
)
# 选项行起首 (gemini 用 "●" U+25CF 标当前选中项)
OPTION_MARKERS = (
    "1. allow once",
    "1. allow",
    "2. allow for",
    "3. no, suggest",
    "allow once",
    "allow for this session",
)

# Shell tool 命令展示行: "? Shell  <command>"
SHELL_TOOL_HEADER_RE = re.compile(r"\?\s+Shell\s+(.+)", re.IGNORECASE)
# Edit/Write tool 展示行: "? Edit  <file_path>" / "? Write  <file_path>"
EDIT_TOOL_HEADER_RE = re.compile(r"\?\s+(Edit|Write|Read|Search)\s+(.+)", re.IGNORECASE)
# fallback: backtick 内的命令
COMMAND_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


@dataclass
class GeminiPending:
    tool_name: str
    tool_input: dict
    tool_kind: str
    description: str
    approve_key: str
    reject_key: str
    raw_excerpt: str


def _tail_text(pane: str, lines: int = 30) -> str:
    return "\n".join(pane.splitlines()[-lines:])


def _looks_like_active_approval(tail: str) -> bool:
    lower = tail.lower()
    has_strong = any(m in lower for m in STRONG_APPROVAL_MARKERS)
    has_option = any(m in lower for m in OPTION_MARKERS)
    return has_strong and has_option


def _active_approval_block(tail: str) -> str:
    """找最后一个 active approval block. gemini 命令展示 box 在 approval marker
    之前 (上面 ╭── 框), 所以 block 取 marker 前 20 行 + marker 后到末尾.

    stale 检测: marker 之后出现 idle UI 行 → 历史 approval."""
    lines = tail.splitlines()
    marker_idx = -1
    # 倒序找最后一个 strong marker (新 approval 优先)
    for i in range(len(lines) - 1, -1, -1):
        if any(m in lines[i].lower() for m in STRONG_APPROVAL_MARKERS):
            marker_idx = i
            break
    if marker_idx < 0:
        return ""

    # block: marker 前 20 行 (含命令展示 box) + marker 后到末尾
    start = max(0, marker_idx - 20)
    block_lines = lines[start:]
    block = "\n".join(block_lines)
    if not _looks_like_active_approval(block):
        return ""

    # stale 检测: marker 之后出现 idle UI 行 → 历史 approval (新一轮已结束)
    stale_markers = (
        "type your message",
        "request cancelled",
        "? for shortcuts",
    )
    after_marker = lines[marker_idx + 1:]
    for line in after_marker:
        lower = line.lower()
        if any(m in lower for m in stale_markers):
            return ""
    return block


def _clean(value: str) -> str:
    value = value.strip()
    # 去掉 box drawing 字符
    value = value.strip("│╭╮╰╯─━ ")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _extract_shell_command(tail: str) -> str:
    """gemini Shell tool: 找 '? Shell  <cmd>' 行. cmd 就在 header 行里抽出来,
    内嵌 box 是冗余展示, 不解析."""
    for line in tail.splitlines():
        m = SHELL_TOOL_HEADER_RE.search(line)
        if m:
            return _clean(m.group(1))
    return ""


def _extract_edit_target(tail: str) -> tuple[str, str]:
    """gemini Edit/Write/Read/Search tool: 找 '? Edit <path>' 行."""
    for line in tail.splitlines():
        m = EDIT_TOOL_HEADER_RE.search(line)
        if m:
            tool = m.group(1).strip().capitalize()
            target = _clean(m.group(2))
            return tool, target
    return "", ""


def parse_gemini_pending(pane: str, agent_id: str = "") -> GeminiPending | None:
    """主入口: pane 全文 → GeminiPending (active approval) 或 None."""
    tail = _tail_text(pane)
    active = _active_approval_block(tail)
    if not active:
        return None

    # 1. Shell command
    cmd = _extract_shell_command(active)
    if cmd:
        return GeminiPending(
            tool_name="Bash",
            tool_input={"command": cmd, "_agent_id": agent_id},
            tool_kind="bash",
            description=f"Bash: {cmd}",
            approve_key="1",
            reject_key="Escape",
            raw_excerpt=active,
        )

    # 2. Edit/Write/Read/Search target file
    edit_tool, target = _extract_edit_target(active)
    if edit_tool and target:
        return GeminiPending(
            tool_name=edit_tool,
            tool_input={"file_path": target, "_agent_id": agent_id},
            tool_kind=edit_tool.lower(),
            description=f"{edit_tool}: {target}",
            approve_key="1",
            reject_key="Escape",
            raw_excerpt=active,
        )

    # 3. fallback: backtick 命令
    for line in active.splitlines():
        m = COMMAND_BACKTICK_RE.search(line)
        if m:
            cmd = _clean(m.group(1))
            if cmd and not cmd.lower().startswith(("yes", "allow", "approve", "no")):
                return GeminiPending(
                    tool_name="Bash",
                    tool_input={"command": cmd, "_agent_id": agent_id},
                    tool_kind="bash",
                    description=f"Bash: {cmd}",
                    approve_key="1",
                    reject_key="Escape",
                    raw_excerpt=active,
                )

    # 4. generic — fail-closed, evaluator 必须给 ask 不能自动 allow
    return GeminiPending(
        tool_name="GeminiApproval",
        tool_input={
            "description": "unknown Gemini approval UI",
            "raw_excerpt": active,
            "_agent_id": agent_id,
        },
        tool_kind="gemini_approval",
        description="Unknown Gemini approval UI",
        approve_key="1",
        reject_key="Escape",
        raw_excerpt=active,
    )
