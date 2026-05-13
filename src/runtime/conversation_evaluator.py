"""
conversation_evaluator — fn_runtime phase 3.

LLM 评估器: 给定 mini_task (bus 派的任务完成产物), 调 gemini -p 评估应当
  /clear (释放 context, 任务干净结束 / 短任务 / 不需后续保留)
  /compact (保摘要清正文, 任务长 / 含关键决策 / 后续可能引用)
  noop (不动, 任务未真完成 / 待用户审 / 评估不确定)

复用 src/task_summarizer.py 调用模式: subprocess + `source ~/rule.sh && gemini -p` + fail-safe 返 None.

事件触发 (HC-A9/G10): conversation_lifecycle.auto_evaluate 在硬门槛通过后调一次, 不周期.
HC-PRE-1 stdlib only. HC-PRE-2 fail-safe: 任何错误返 None → caller fallback 规则.
 agent-gov: LLM 输出仅 action 决策, 不修改 config / threshold.

API:
  evaluate(agent_id, mini_task, timeout_sec=30) -> Optional[dict]
    返 {"action": "clear|compact|noop", "reason": str, "confidence": float, "raw": str}
    任何错误 (timeout / disabled / parse fail) 返 None.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional


# 复用 conversation_lifecycle 的 config loader (mtime hot reload)
try:
    from src.runtime.conversation_lifecycle import load_config as _load_lifecycle_config
except ImportError:
    try:
        from runtime.conversation_lifecycle import load_config as _load_lifecycle_config
    except ImportError:
        def _load_lifecycle_config() -> dict:
            return {}


_VALID_ACTIONS = ("clear", "compact", "noop")


def _shell_quote(s: str) -> str:
    """安全 shell 引用, 防命令注入. 复用 task_summarizer 模式."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    s = str(s).replace("\r", " ").replace("\t", " ")
    return s[:n]


PROMPT_TEMPLATE = """你是 pre 总线的 conversation 生命周期评估器.

下面是某个 agent 刚完成的一项 bus 任务 (mini_task). 请决定该 agent 接下来 conversation 应该执行哪个动作:

- clear: 释放 context (推荐: 任务干净结束 / 短任务 / 后续无需引用本任务上下文)
- compact: 保留摘要清空正文 (推荐: 任务较长 / 含关键决策或参数 / 后续步骤可能引用)
- noop: 不动作 (推荐: 任务未真完成 / 等用户审核 / 评估不确定)

mini_task 内容:
- agent_id: {agent_id}
- title: {title}
- intent: {intent}
- result: {result}
- parent_dispatch_id: {parent_dispatch_id}
- duration_sec: {duration_sec}
- last_action: {last_action}
- elapsed_since_last_cleanup_sec: {elapsed_since_last_cleanup_sec}

输出格式严格 (单行 JSON, 不要 markdown 代码块, 不要解释):
{{"action": "clear|compact|noop", "reason": "<20 字内中文>", "confidence": 0.0-1.0}}
"""


def _build_prompt(agent_id: str, mini_task: dict, ctx: Optional[dict] = None) -> str:
    """构建 LLM prompt. 截断每字段防止过长 / 注入."""
    mt = mini_task or {}
    ctx = ctx or {}
    return PROMPT_TEMPLATE.format(
        agent_id=_truncate(agent_id, 128),
        title=_truncate(mt.get("title") or "(无)", 200),
        intent=_truncate(mt.get("intent") or mt.get("description") or "(无)", 600),
        result=_truncate(mt.get("result") or mt.get("summary") or "(无)", 800),
        parent_dispatch_id=_truncate(mt.get("parent_dispatch_id") or "", 64),
        duration_sec=_truncate(str(mt.get("duration_sec") or "?"), 16),
        last_action=_truncate(ctx.get("last_action") or "(无)", 32),
        elapsed_since_last_cleanup_sec=_truncate(
            str(ctx.get("elapsed_since_last_cleanup_sec") if ctx.get("elapsed_since_last_cleanup_sec") is not None else "?"), 16
        ),
    )


def _parse_response(raw: str) -> Optional[dict]:
    """解析 gemini 输出, 提取 single-line JSON. fail → None."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    m = re.search(r"\{[^{}]*\"action\"[^{}]*\}", text)
    candidate = m.group(0) if m else text.splitlines()[0].strip() if text.splitlines() else ""
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    action = obj.get("action")
    if action not in _VALID_ACTIONS:
        return None
    reason = str(obj.get("reason") or "")[:60]
    try:
        conf = float(obj.get("confidence") if obj.get("confidence") is not None else 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < 0.0:
        conf = 0.0
    if conf > 1.0:
        conf = 1.0
    return {"action": action, "reason": reason, "confidence": conf, "raw": text[:400]}


def _llm_config() -> dict:
    """从 conversation_lifecycle config 读 llm_evaluator 子节. fail-safe → defaults."""
    cfg = _load_lifecycle_config() or {}
    sub = cfg.get("llm_evaluator") or {}
    return {
        "enabled": bool(sub.get("enabled", False)),
        "provider": str(sub.get("provider", "gemini")),
        "model": str(sub.get("model", "")),
        "timeout_sec": int(sub.get("timeout_sec", 90)),
        "fallback_to_rule_on_error": bool(sub.get("fallback_to_rule_on_error", True)),
    }


def _call_gemini(prompt: str, timeout_sec: int = 30, model: str = "") -> Optional[str]:
    """复用 task_summarizer 模式: source ~/rule.sh && gemini -p <quoted> -o text."""
    if not prompt:
        return None
    if model:
        cmd = f'source ~/rule.sh && gemini -m {_shell_quote(model)} -p {_shell_quote(prompt)} -o text'
    else:
        cmd = f'source ~/rule.sh && gemini -p {_shell_quote(prompt)} -o text'
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=max(5, timeout_sec),
        )
        if result.returncode != 0:
            return None
        out = (result.stdout or "").strip()
        return out or None
    except subprocess.TimeoutExpired:
        return None
    except (subprocess.SubprocessError, OSError):
        return None


def evaluate(agent_id: str, mini_task: Optional[dict] = None,
             ctx: Optional[dict] = None,
             timeout_sec: Optional[int] = None,
             provider_override: Optional[str] = None) -> Optional[dict]:
    """
    LLM 评估入口. 任何错误 / disabled → 返 None (caller fallback 规则).

    参数:
      agent_id: 目标 agent
      mini_task: bus 任务产物 dict (title/intent/result/parent_dispatch_id/...)
      ctx: 可选上下文 (last_action / elapsed_since_last_cleanup_sec)
      timeout_sec: 覆盖 config

    返:
      None (LLM disabled / failed / parse error)
      或 {"action": "clear|compact|noop", "reason": str, "confidence": float (0-1),
          "raw": str (≤400 chars), "elapsed_ms": int, "provider": str}
    """
    cfg = _llm_config()
    if not cfg["enabled"]:
        return None
    provider = provider_override or cfg["provider"]
    if provider != "gemini":
        # 当前只实现 gemini, 其他 provider 后续扩展
        return None
    if not mini_task:
        return None

    timeout = int(timeout_sec) if timeout_sec else cfg["timeout_sec"]
    prompt = _build_prompt(agent_id, mini_task, ctx)

    t0 = time.time()
    raw = _call_gemini(prompt, timeout_sec=timeout, model=cfg["model"])
    elapsed_ms = int((time.time() - t0) * 1000)
    if not raw:
        return None
    parsed = _parse_response(raw)
    if not parsed:
        return None
    parsed["elapsed_ms"] = elapsed_ms
    parsed["provider"] = provider
    return parsed
