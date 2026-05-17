"""codex -p 审查器 — 把单条 ask 翻成一条 proposal.

输入: U1 extract 输出的 ask_entry (含 cmd/cwd/reason/邻居/transcript)
输出: proposal dict, schema:
  {
    ask_pattern, original_reason, target_layer (B|C),
    action (whitelist|add_rule|update_rules_md|keep_ask),
    rule_patch_draft, user_question, risk_note
  }

per-ask 调用 — 一条 fail 不带其他. fallback 一律 keep_ask, 不丢条目.

provider 抽象: subprocess runner 可注入 (单测 mock 不真跑 codex).
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

DEFAULT_TIMEOUT = 90
PROPOSAL_FIELDS = (
    "ask_pattern",
    "original_reason",
    "target_layer",
    "action",
    "rule_patch_draft",
    "user_question",
    "risk_note",
)
VALID_LAYERS = ("B", "C")
VALID_ACTIONS = ("whitelist", "add_rule", "update_rules_md", "keep_ask")

Runner = Callable[[str], tuple[int, str, str]]


def _build_prompt(
    ask_entry: dict,
    rules_py_excerpt: str = "",
    rules_md: str = "",
) -> str:
    cmd = ask_entry.get("cmd", "")
    cwd = ask_entry.get("cwd", "")
    reason = ask_entry.get("reason", "")
    source = ask_entry.get("source", "")
    neighbors = ask_entry.get("neighbor_jsonl") or []
    transcript = ask_entry.get("transcript_excerpt") or []

    neighbor_lines = []
    for n in neighbors[:20]:
        inp = n.get("input") if isinstance(n.get("input"), dict) else {}
        ncmd = (inp.get("command") if inp else None) or n.get("command_preview", "")
        neighbor_lines.append(
            f"  [{n.get('ts','?')}] {str(n.get('decision','?')):<5} "
            f"{n.get('tool','?')} — {str(ncmd)[:120]}"
        )
    neighbor_text = "\n".join(neighbor_lines) or "  (none)"

    transcript_lines = []
    for t in transcript[:20]:
        transcript_lines.append(
            f"  [{t.get('timestamp', '?')}] {t.get('type', '?')}: "
            f"{str(t.get('message',''))[:160]}"
        )
    transcript_text = "\n".join(transcript_lines) or "  (none)"

    return f"""你是 pre 项目的 governor 规则改进 reviewer. 给定一条之前被 governor 判为 **ASK** 的请求, 决定是否应该:
- 加白名单 (rules.py 字面规则, Layer C) — 落地后 governor 不再问
- 改 rules.md 文本规则 (Layer B) — 引导 governor LLM 决策
- 加新的 ASK 模式 (确认这条确实危险, 不要意外放行)
- 保持现状 (keep_ask)

## Layer A — 你看到的 context (帮你理解, 但落地规则可能看不到)

**Ask 请求**:
- 命令: `{cmd}`
- cwd: `{cwd}`
- governor 原因: {reason}
- 触发源: {source}   (governor = LLM 灰区 / governor_no_cache = 供应链审查)

**同 session 同 cwd ±5min 的 jsonl 邻居**:
{neighbor_text}

**对应 Claude Code transcript ts 附近 ±10 条**:
{transcript_text}

**当前 src/rules.py 关键节选**:
```python
{rules_py_excerpt[:2000]}
```

**当前 pre/rules.md (项目级 LLM 规则)**:
{rules_md[:1500]}

## 输出约束 (硬性)

输出 **单个 JSON 对象**, 字段:

```json
{{
  "ask_pattern": "<本次 ask 的归纳模式 — 不是原 cmd, 而是可复用模板>",
  "original_reason": "{reason}",
  "target_layer": "B" | "C",
  "action": "whitelist" | "add_rule" | "update_rules_md" | "keep_ask",
  "rule_patch_draft": "<unified diff 草案, B 改 rules.md / C 改 rules.py>",
  "user_question": "<一句话给用户决断的问题>",
  "risk_note": "<风险说明 + 此 patch 落地后可能误放行的边界场景>"
}}
```

### target_layer 选择硬约束

- **C (rules.py 字面规则)**: 落地的 rule **只能依赖 cmd 字符串模式** (前缀 / 正则).
  governor 决策时**看不到** cwd / history. 如果你的判断"应 allow"依赖 cwd 或
  历史上下文 — 选 B 而非 C, 否则规则跨 cwd 误放行.
- **B (rules.md 文本规则)**: 可以引用 cmd + cwd 模式 + 历史关键词.
  governor LLM 决策时能读 rules.md + 最近 5 条 transcript 摘要.
- 不引用 transcript 细节 / mode / env / agent_id 等 governor 看不到的字段.

### action 选择

- `whitelist`: 加 cmd 前缀/正则到 `_BASH_SAFE_PREFIXES` / `_INLINE_SAFE_RE` (Layer C)
- `add_rule`: 加新的 ASK 或黑名单 (避免误放行) (Layer C)
- `update_rules_md`: 改 system/global/project rules.md (Layer B)
- `keep_ask`: 这条不安全自动化, 保持人工 ask. user_question 解释为什么.

只输出 JSON, 不要前缀/后缀, 不要 markdown code fence.
"""


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    return s.strip()


def _fallback_keep_ask(ask_entry: dict, why: str) -> dict:
    return {
        "ask_pattern": str(ask_entry.get("cmd", ""))[:200],
        "original_reason": ask_entry.get("reason", ""),
        "target_layer": "B",
        "action": "keep_ask",
        "rule_patch_draft": "",
        "user_question": f"codex review unavailable ({why}). 这条要不要手动改?",
        "risk_note": f"fallback: {why}",
    }


def _parse_proposal(raw: str, ask_entry: dict) -> dict:
    s = _strip_code_fence(raw)
    if not s:
        return _fallback_keep_ask(ask_entry, "empty output")
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        return _fallback_keep_ask(ask_entry, f"parse error: {e}")
    if not isinstance(obj, dict):
        return _fallback_keep_ask(ask_entry, "non-dict output")

    for f in PROPOSAL_FIELDS:
        obj.setdefault(f, "")
    if obj.get("target_layer") not in VALID_LAYERS:
        obj["target_layer"] = "B"
    if obj.get("action") not in VALID_ACTIONS:
        obj["action"] = "keep_ask"
    return obj


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _run_codex(
    prompt: str, *, timeout: int, cwd: str | None = None
) -> tuple[int, str, str]:
    """跑 codex exec subprocess. 复用 governor.py 的 source ~/rule.sh + 引用模式."""
    cmd = (
        f"source ~/rule.sh && codex exec --skip-git-repo-check "
        f"{_shell_quote(prompt)}"
    )
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", f"codex not found: {e}"


def review_ask(
    ask_entry: dict,
    *,
    rules_py_excerpt: str = "",
    rules_md: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | None = None,
    runner: Runner | None = None,
) -> dict:
    """对单条 ask_entry 调 codex 输出 proposal."""
    prompt = _build_prompt(
        ask_entry, rules_py_excerpt=rules_py_excerpt, rules_md=rules_md
    )
    run = runner or (lambda p: _run_codex(p, timeout=timeout, cwd=cwd))
    rc, stdout, stderr = run(prompt)
    if rc != 0:
        return _fallback_keep_ask(
            ask_entry, f"codex rc={rc}: {stderr.strip()[:120]}"
        )
    return _parse_proposal(stdout, ask_entry)


def review_batch(
    ask_entries: list[dict],
    *,
    rules_py_excerpt: str = "",
    rules_md: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | None = None,
    runner: Runner | None = None,
) -> list[dict]:
    """顺序 review 所有 ask. 单条 fail 走 fallback, 不影响其他."""
    return [
        review_ask(
            ae,
            rules_py_excerpt=rules_py_excerpt,
            rules_md=rules_md,
            timeout=timeout,
            cwd=cwd,
            runner=runner,
        )
        for ae in ask_entries
    ]
