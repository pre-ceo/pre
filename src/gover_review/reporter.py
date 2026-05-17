"""INFO finding 写入 + 用户回答 polling + 改进报告生成 (B2 文件协议).

流程:
  1. agent 写 INFO-gover-improve-cycle-N.md 到 <workdir>/pre/findings/
     - 每个 proposal 一对 Q{i} / A{i} 段
     - 算 sha256 存进 state.pending_sha256
  2. agent polling 30s, sha256 不变 → continue, 变了 → parse_answers
  3. agent 把回答 + proposal 写到 dev-workflow/findings/YYMMDD-cycle-N.md
  4. INFO 移到 findings/processed/

用户回答格式 (每个 A{i} 段下一行):
  accept / reject / skip / modify: <说明>
  (大小写不敏感)
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_ANSWER_RE = re.compile(
    r"^\s*(accept|reject|skip|modify:.*)\s*$", re.IGNORECASE
)
_ANSWER_HEADER_RE = re.compile(r"^###\s+A(\d+)\s*$")

DEFAULT_POLL_SECONDS = 30.0
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


def compute_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def format_finding(
    cycle_n: int,
    since_iso: str,
    until_iso: str,
    proposals: list[dict],
) -> str:
    n = len(proposals)
    lines = [
        f"# gover_review cycle {cycle_n} — {n} 个 ask 待审",
        "",
        f"- 时间窗: `{since_iso}` → `{until_iso}`",
        f"- proposal 数: {n}",
        "",
        "## 回答方式",
        "",
        "在每个 `### A{i}` 标题**下面追加一行**:",
        "- `accept` — 同意, agent 把 patch 写进改进报告供你手动 apply",
        "- `reject` — 不同意, 不落地",
        "- `modify: <说明>` — 接受但要改, <说明> 写改法",
        "- `skip` — 这条暂不处理",
        "",
        "agent polling 文件 sha256, 变化 → 解析 → 写改进报告 + 移 INFO 到 processed/.",
        "**不要**改 Q 段或新增 ## 标题.",
        "",
        "---",
        "",
    ]
    for i, p in enumerate(proposals, 1):
        patch = (p.get("rule_patch_draft") or "").rstrip()
        if not patch:
            patch = "(empty — keep_ask 或 patch 缺失)"
        lines.extend(
            [
                f"### Q{i}: {p.get('ask_pattern', '?')}",
                "",
                f"- **target_layer**: {p.get('target_layer', '?')}",
                f"- **action**: {p.get('action', '?')}",
                f"- **original_reason**: {p.get('original_reason', '?')}",
                f"- **risk_note**: {p.get('risk_note', '?')}",
                "",
                f"**user_question**: {p.get('user_question', '?')}",
                "",
                "**rule_patch_draft**:",
                "",
                "```diff",
                patch,
                "```",
                "",
                f"### A{i}",
                "<!-- 在下面写一行: accept / reject / modify: <说明> / skip -->",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines)


def write_finding(
    workdir: Path | str,
    cycle_n: int,
    since_iso: str,
    until_iso: str,
    proposals: list[dict],
) -> tuple[Path, str]:
    """写 INFO finding, 返 (path, sha256)."""
    fdir = Path(workdir) / "pre" / "findings"
    fdir.mkdir(parents=True, exist_ok=True)
    fpath = fdir / f"INFO-gover-improve-cycle-{cycle_n}.md"
    fpath.write_text(
        format_finding(cycle_n, since_iso, until_iso, proposals)
    )
    return fpath, compute_sha256(fpath)


def parse_answers(finding_path: Path | str) -> dict[int, str]:
    """读 INFO finding, 提取每个 A{n} 段下用户写的第一行 matching 答案.

    没填的 A 段不进 dict.
    """
    text = Path(finding_path).read_text()
    answers: dict[int, str] = {}
    current_n: int | None = None
    for line in text.splitlines():
        h_a = _ANSWER_HEADER_RE.match(line)
        if h_a:
            current_n = int(h_a.group(1))
            continue
        if current_n is None:
            continue
        if line.startswith("### "):
            current_n = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        m = _ANSWER_RE.match(line)
        if m:
            answers[current_n] = m.group(1).strip()
            current_n = None
    return answers


def is_user_answered(
    finding_path: Path | str, original_sha256: str
) -> bool:
    p = Path(finding_path)
    if not p.exists():
        return False
    return compute_sha256(p) != original_sha256


def wait_for_user_answer(
    finding_path: Path | str,
    original_sha256: str,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    timeout_seconds: float | None = None,
    sleeper: Sleeper = time.sleep,
    clock: Clock = time.monotonic,
) -> dict[int, str] | None:
    """polling 直到 sha256 变化 → 返答案; 超时 → None."""
    p = Path(finding_path)
    start = clock()
    while True:
        if is_user_answered(p, original_sha256):
            return parse_answers(p)
        if (
            timeout_seconds is not None
            and (clock() - start) >= timeout_seconds
        ):
            return None
        sleeper(poll_seconds)


def _classify_answer(ans: str) -> str:
    a = ans.strip().lower()
    if a.startswith("modify"):
        return "modify"
    if a in ("accept", "reject", "skip"):
        return a
    return "no_answer"


def format_report(
    cycle_n: int,
    since_iso: str,
    until_iso: str,
    proposals: list[dict],
    answers: dict[int, str],
    *,
    now: datetime | None = None,
) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    summary = {
        "accept": 0,
        "reject": 0,
        "modify": 0,
        "skip": 0,
        "no_answer": 0,
    }
    for i in range(1, len(proposals) + 1):
        kind = _classify_answer(answers.get(i, ""))
        summary[kind] += 1

    lines = [
        f"# gover_review cycle {cycle_n} — 改进报告",
        "",
        f"- 时间窗: `{since_iso}` → `{until_iso}`",
        f"- proposal 数: {len(proposals)}",
        f"- 完成 ts: `{now.isoformat()}`",
        "",
        "## 用户决断 summary",
        "",
        f"- accept: {summary['accept']}",
        f"- reject: {summary['reject']}",
        f"- modify: {summary['modify']}",
        f"- skip: {summary['skip']}",
        f"- no_answer: {summary['no_answer']}",
        "",
        "## 详情",
        "",
    ]
    for i, p in enumerate(proposals, 1):
        ans = answers.get(i, "(no answer — fallback keep_ask)")
        patch = (p.get("rule_patch_draft") or "").rstrip() or "(empty)"
        lines.extend(
            [
                f"### #{i} — {ans}",
                "",
                f"- target_layer: {p.get('target_layer', '?')}",
                f"- action: {p.get('action', '?')}",
                f"- ask_pattern: `{p.get('ask_pattern', '?')}`",
                f"- user_question: {p.get('user_question', '?')}",
                f"- risk_note: {p.get('risk_note', '?')}",
                "",
                "**待 apply patch**:",
                "",
                "```diff",
                patch,
                "```",
                "",
            ]
        )
    lines.extend(["## 落地 checklist (用户手动 apply)", ""])
    apply_items = []
    for i, p in enumerate(proposals, 1):
        kind = _classify_answer(answers.get(i, ""))
        if kind in ("accept", "modify"):
            tgt = p.get("target_layer", "?")
            file_hint = "src/rules.py" if tgt == "C" else "pre/rules.md / system.md / global.md"
            apply_items.append(
                f"- [ ] apply #{i} patch ({kind}) → `{file_hint}` (target_layer={tgt})"
            )
    if apply_items:
        lines.extend(apply_items)
    else:
        lines.append("- (无 accept/modify, 无需 apply)")
    lines.append("")
    return "\n".join(lines)


def write_report(
    dev_workflow_findings_dir: Path | str,
    cycle_n: int,
    since_iso: str,
    until_iso: str,
    proposals: list[dict],
    answers: dict[int, str],
    *,
    now: datetime | None = None,
) -> Path:
    if now is None:
        now = datetime.now(timezone.utc)
    fdir = Path(dev_workflow_findings_dir)
    fdir.mkdir(parents=True, exist_ok=True)
    fname = f"{now.strftime('%y%m%d')}-cycle-{cycle_n}.md"
    fpath = fdir / fname
    fpath.write_text(
        format_report(
            cycle_n, since_iso, until_iso, proposals, answers, now=now
        )
    )
    return fpath


def move_to_processed(finding_path: Path | str) -> Path:
    """把 INFO finding 移到 findings/processed/."""
    src = Path(finding_path)
    dst_dir = src.parent / "processed"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), str(dst))
    return dst
