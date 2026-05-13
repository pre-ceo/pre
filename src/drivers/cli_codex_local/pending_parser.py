"""Codex approval 文本解析 — 保守, tail-only, fail-closed.

fixture-driven + stale 检测, 解不出 command/path 时返 CodexApproval
generic, 让 evaluator 走 governor/ask 而不是自动 allow.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


# 弱 marker (出现 + option marker 才算 active)
APPROVAL_MARKERS = (
    "wants approval",
    "needs permission",
    "would you like to run the following command",
    "do you want",
    "permission",
    "confirm",
)

# 强 marker (锚定 active block 起点)
STRONG_APPROVAL_MARKERS = (
    "wants approval",
    "needs permission",
    "would you like to run the following command",
    "do you want",
)

OPTION_MARKERS = (
    "yes",
    "allow",
    "approve",
    "deny",
    "reject",
    "cancel",
    "escape",
    "esc",
)

COMMAND_LABEL_PATTERN = re.compile(r"(?:command|cmd|bash)\s*:\s*(.+)", re.IGNORECASE)
COMMAND_PROMPT_PATTERN = re.compile(r"^\s*\$\s+(.+)$")
COMMAND_BACKTICK_PATTERN = re.compile(r"`([^`\n]+)`")

PATH_PATTERNS = (
    re.compile(r"(?:file|path)\s*:\s*([~/A-Za-z0-9_./ -]+)", re.IGNORECASE),
    re.compile(r"(?:edit|write|create)\s+([~/A-Za-z0-9_./-]+\.[A-Za-z0-9_./-]+)", re.IGNORECASE),
)


@dataclass
class CodexPending:
    tool_name: str
    tool_input: dict
    tool_kind: str
    description: str
    approve_key: str
    reject_key: str
    raw_excerpt: str


def _tail_text(pane: str, lines: int = 24) -> str:
    return "\n".join(pane.splitlines()[-lines:])


def _looks_like_active_approval(tail: str) -> bool:
    lower = tail.lower()
    has_approval = any(m in lower for m in APPROVAL_MARKERS)
    has_option = any(m in lower for m in OPTION_MARKERS)
    return has_approval and has_option


def _active_approval_block(tail: str) -> str:
    """找最后一个 active approval block. Codex 回到正常输入/工作 UI 后,
    tail 里的旧 approval 文本被视为历史 (stale), 不能触发自动批准."""
    lines = tail.splitlines()
    marker_idx = -1
    # 优先取最后一个强 marker (active block)
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(m in lower for m in STRONG_APPROVAL_MARKERS):
            marker_idx = i
    if marker_idx < 0:
        # fallback 弱 marker
        for i, line in enumerate(lines):
            lower = line.lower()
            if any(m in lower for m in APPROVAL_MARKERS):
                marker_idx = i
    if marker_idx < 0:
        return ""

    block_lines = lines[marker_idx:]
    block = "\n".join(block_lines)
    if not _looks_like_active_approval(block):
        return ""

    # stale 检测: marker 之后出现正常输入/工作状态行 → 历史 approval
    stale_markers = (
        " tab to queue message",
        " context left",
        "• working",
    )
    for line in block_lines[1:]:
        lower = line.lower()
        if any(m in lower for m in stale_markers):
            return ""
        stripped = line.strip()
        # 非 "› N." 选项的 "› " 输入提示行 = 已回到 prompt
        if stripped.startswith("› ") and not re.match(r"^›\s*\d+\.", stripped):
            return ""
    return block


def _clean_extracted_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _extract_prompt_command(lines: list[str]) -> str:
    """`$ cmd` 形式, 支持续行 (直到空行 / 选项行 / 提示行)."""
    candidate = ""
    for i, line in enumerate(lines):
        m = COMMAND_PROMPT_PATTERN.search(line.strip())
        if not m:
            continue
        parts = [m.group(1).strip()]
        for cont in lines[i + 1:]:
            stripped = cont.strip()
            if not stripped:
                break
            lower = stripped.lower()
            if re.match(r"^(›\s*)?\d+\.", stripped):
                break
            if lower.startswith(("press enter", "esc to cancel", "yes,", "no,")):
                break
            parts.append(stripped)
        cmd = _clean_extracted_value(" ".join(parts))
        if cmd and not cmd.lower().startswith(("yes", "allow", "approve")):
            candidate = cmd
    return candidate


def _extract_command(tail: str) -> str:
    lines = tail.splitlines()

    # 1) "command:" / "cmd:" / "bash:" 显式 label
    for line in lines:
        s = line.strip()
        if not s:
            continue
        m = COMMAND_LABEL_PATTERN.search(s)
        if m:
            cmd = _clean_extracted_value(m.group(1))
            if cmd and not cmd.lower().startswith(("yes", "allow", "approve")):
                return cmd

    # 2) `$ cmd` 形式
    prompt_cmd = _extract_prompt_command(lines)
    if prompt_cmd:
        return prompt_cmd

    # 3) reverse 找 `$ cmd`
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        m = COMMAND_PROMPT_PATTERN.search(s)
        if m:
            cmd = _clean_extracted_value(m.group(1))
            if cmd and not cmd.lower().startswith(("yes", "allow", "approve")):
                return cmd

    # 4) backtick `cmd`
    for line in lines:
        s = line.strip()
        if not s:
            continue
        m = COMMAND_BACKTICK_PATTERN.search(s)
        if m:
            cmd = _clean_extracted_value(m.group(1))
            if cmd and not cmd.lower().startswith(("yes", "allow", "approve")):
                return cmd
    return ""


def _extract_path(tail: str) -> tuple[str, str]:
    lower = tail.lower()
    mode = "Edit"
    if "create" in lower or "write" in lower:
        mode = "Write"
    for line in reversed(tail.splitlines()):
        for pat in PATH_PATTERNS:
            m = pat.search(line)
            if m:
                return mode, m.group(1).strip().strip("'\"")
    return mode, ""


def parse_codex_pending(pane: str, agent_id: str = "") -> CodexPending | None:
    """主入口: pane 全文 → CodexPending (active approval) 或 None."""
    tail = _tail_text(pane)
    active = _active_approval_block(tail)
    if not active:
        return None

    cmd = _extract_command(active)
    if cmd:
        return CodexPending(
            tool_name="Bash",
            tool_input={"command": cmd, "_agent_id": agent_id},
            tool_kind="bash",
            description=f"Bash: {cmd}",
            approve_key="1",
            reject_key="Escape",
            raw_excerpt=active,
        )

    mode, file_path = _extract_path(active)
    if file_path:
        return CodexPending(
            tool_name=mode,
            tool_input={"file_path": file_path, "_agent_id": agent_id},
            tool_kind=mode.lower(),
            description=f"{mode}: {file_path}",
            approve_key="1",
            reject_key="Escape",
            raw_excerpt=active,
        )

    # generic — fail-closed, evaluator 必须给 ask 不能自动 allow
    return CodexPending(
        tool_name="CodexApproval",
        tool_input={
            "description": "unknown Codex approval UI",
            "raw_excerpt": active,
            "_agent_id": agent_id,
        },
        tool_kind="codex_approval",
        description="Unknown Codex approval UI",
        approve_key="1",
        reject_key="Escape",
        raw_excerpt=active,
    )
