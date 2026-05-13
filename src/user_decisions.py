"""
user_decisions — agent-ceo stop hook 后 LLM 提炼"需要用户决断"事项 (user 直接需求).

[agent-ceo-only hack 自 待 ≥3 agent 升级通用 — 跟 同模式]

事件触发: stop_analyzer 末尾调 user_decisions_hook(agent_id, transcript_path).
HC-PRE-1 stdlib only. HC-PRE-2 fail-safe (任何错误 silent skip 不阻 stop_hook).
HC-A9/G10 anti-polling: 单次触发, cooldown 60s 防短期重复.

API:
  load_config() -> dict
  is_enabled_for(agent_id) -> bool
  user_decisions_hook(agent_id, transcript_path) -> str | None # decision_id or None
  list_decisions(status=None, limit=50) -> list[dict]
  get_decision(decision_id) -> dict | None
  resolve_decision(decision_id, decision, note?) -> bool
  dismiss_decision(decision_id) -> bool
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from common.paths import PRE_RULE_ROOT, PRE_LOG_ROOT
from typing import Optional

# token: lazy resolve from ~/.pre/env via token_resolver (PR3)
try:
    from src.common.token_resolver import resolve as _resolve_token  # hook context
except ImportError:
    from common.token_resolver import resolve as _resolve_token  # master context

# Loopback master call: direct, bypass proxy env (Surge etc.)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


_RULE_PATH = Path(os.environ.get(
    "PRE_USER_DECISIONS_RULE",
    str(Path(PRE_RULE_ROOT) / "user_decisions.json"),
))
_LOG_DIR = Path(os.environ.get(
    "PRE_LOG_DIR",
    PRE_LOG_ROOT,
))
_OUT_DIR = _LOG_DIR / "user_decisions"
_COOLDOWN_PATH = _OUT_DIR / "_cooldown_state.json"

_DEFAULT_CONFIG = {
    "version": 1,
    "enabled": False,
    "include_agents": [],
    "llm_provider": "gemini",
    "llm_timeout_sec": 60,
    "transcript_lines": 100,
    "cooldown_sec": 60,
    "min_questions_to_persist": 1,
}

_CACHE: dict = {"mtime": 0.0, "cfg": None}

# decision_id 校验 (防 path traversal API 用)
_DECISION_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def load_config() -> dict:
    """mtime hot reload, fail-safe default disabled."""
    try:
        if not _RULE_PATH.exists():
            return dict(_DEFAULT_CONFIG)
        mtime = _RULE_PATH.stat().st_mtime
        if _CACHE["cfg"] is not None and _CACHE["mtime"] == mtime:
            return _CACHE["cfg"]
        with open(_RULE_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(_DEFAULT_CONFIG)
        merged.update(cfg)
        _CACHE["cfg"] = merged
        _CACHE["mtime"] = mtime
        return merged
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_CONFIG)


def is_enabled_for(agent_id: str) -> bool:
    cfg = load_config()
    if not cfg.get("enabled"):
        return False
    return agent_id in (cfg.get("include_agents") or [])


def _read_transcript_tail(transcript_path: str, n_lines: int) -> str:
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-n_lines:] if len(lines) > n_lines else lines
        return "".join(tail)[-8192:]  # cap 8KB
    except (OSError, ValueError):
        return ""


def _check_cooldown(agent_id: str, cooldown_sec: float) -> bool:
    """返 True 表示在 cooldown 内 (跳过). 写新 ts 时返 False."""
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_OUT_DIR), 0o700)
        except OSError:
            pass
        state = {}
        if _COOLDOWN_PATH.exists():
            try:
                with open(_COOLDOWN_PATH, encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                state = {}
        last = float(state.get(agent_id, 0.0))
        now = time.time()
        if (now - last) < cooldown_sec:
            return True  # in cooldown
        state[agent_id] = now
        with open(_COOLDOWN_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
        try:
            os.chmod(str(_COOLDOWN_PATH), 0o600)
        except OSError:
            pass
        return False
    except OSError:
        return False  # fail-safe: 不阻


_LLM_PROMPT = """读以下 agent-ceo agent 的 transcript 末尾片段, 提炼出**需要用户 (user) 决断**的问题.

仅输出 JSON, 严格 schema (无 markdown 代码块, 直接 JSON):
{"summary":"<1-2句中文整体摘要>","questions":[{"question":"...","options":["...","..."],"urgency":"high"|"normal","context":"..."}]}

字段要求:
- summary: 1-2 句中文, 整段 transcript 在做什么的总览
- question: 1-2 句中文, 具体待决问题, 易理解避免技术 jargon
- options: 备选项列表 ≥2 项, 让用户能直接选 (e.g. ["批准 Phase B 启动", "暂停等更多数据"])
- urgency: high (阻塞 agent-ceo 主流程必决) | normal (可延后)
- context: 1-2 句背景 (谁派的 / dispatch_id / 时间敏感性 / 关联文件)

若无需用户决断的事 → 返 {"summary":"...","questions":[]}.

transcript 末尾片段:
---
{transcript_excerpt}
---
"""


def _call_llm(provider: str, prompt: str, timeout_sec: float) -> Optional[dict]:
    """调 gemini / codex cli subprocess. fail-safe 返 None."""
    if provider == "gemini":
        cmd = ["gemini", "-p", prompt]
    elif provider == "codex":
        cmd = ["codex", "exec", prompt]
    else:
        return None
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_sec, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # 抠 JSON: 第一个 { 到末配对 }
    s = out.find("{")
    if s < 0:
        return None
    depth = 0
    end = -1
    for i in range(s, len(out)):
        if out[i] == "{":
            depth += 1
        elif out[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        return json.loads(out[s:end])
    except (json.JSONDecodeError, ValueError):
        return None


def _decision_path(decision_id: str) -> Optional[Path]:
    if not _DECISION_ID_RE.match(decision_id):
        return None
    return _OUT_DIR / f"{decision_id}.json"


def _write_decision(decision: dict) -> bool:
    decision_id = decision.get("decision_id", "")
    p = _decision_path(decision_id)
    if p is None:
        return False
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(_OUT_DIR), 0o700)
        except OSError:
            pass
        with open(p, "w", encoding="utf-8") as f:
            json.dump(decision, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def user_decisions_hook(agent_id: str, transcript_path: str) -> Optional[str]:
    """事件入口: stop_analyzer 末尾调. fail-safe.
    返 decision_id (写 file 成功) or None (skip / 失败).
    [agent-ceo-only hack 自 ]
    """
    try:
        if not is_enabled_for(agent_id):
            return None
        cfg = load_config()
        if _check_cooldown(agent_id, float(cfg.get("cooldown_sec", 60))):
            return None
        excerpt = _read_transcript_tail(
            transcript_path, int(cfg.get("transcript_lines", 100)))
        if not excerpt:
            return None
        prompt = _LLM_PROMPT.replace("{transcript_excerpt}", excerpt)
        parsed = _call_llm(
            cfg.get("llm_provider", "gemini"),
            prompt,
            float(cfg.get("llm_timeout_sec", 60)),
        )
        if not isinstance(parsed, dict):
            return None
        questions = parsed.get("questions") or []
        if not isinstance(questions, list):
            return None
        # G3 vacuous truth: 严禁空 questions 写 file (浪费存储)
        min_q = int(cfg.get("min_questions_to_persist", 1))
        if len(questions) < min_q:
            return None
        decision_id = uuid.uuid4().hex
        decision = {
            "decision_id": decision_id,
            "agent_id": agent_id,
            "ts": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "transcript_excerpt": excerpt[-4096:],
            "summary": (parsed.get("summary") or "")[:500],
            "questions": questions[:20],  # cap 20 questions per decision
            "status": "pending",
            "resolution": None,
        }
        if _write_decision(decision):
            return decision_id
        return None
    except Exception:  # noqa: BLE001 — fail-safe
        return None


# ---------- API helpers (server.py 调) ----------

def list_decisions(status: Optional[str] = None,
                    limit: int = 50) -> list[dict]:
    """列表查询. status filter: pending|resolved|dismissed|None(all). 按 ts DESC."""
    if not _OUT_DIR.exists():
        return []
    out: list[dict] = []
    try:
        files = sorted(_OUT_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        return []
    for p in files:
        if p.name.startswith("_"):
            continue  # _cooldown_state.json etc.
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if status and d.get("status") != status:
            continue
        out.append({
            "decision_id": d.get("decision_id"),
            "agent_id": d.get("agent_id"),
            "ts": d.get("ts"),
            "summary": d.get("summary"),
            "questions_count": len(d.get("questions") or []),
            "urgency_max": _max_urgency(d.get("questions") or []),
            "status": d.get("status"),
        })
        if len(out) >= limit:
            break
    return out


def _max_urgency(questions: list) -> str:
    for q in questions:
        if isinstance(q, dict) and q.get("urgency") == "high":
            return "high"
    return "normal"


def get_decision(decision_id: str) -> Optional[dict]:
    p = _decision_path(decision_id)
    if p is None or not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def resolve_decision(decision_id: str, decision: str,
                       note: str = "") -> bool:
    """标 resolved + 写 resolution 字段. 不删原 questions (audit trail).
    [user 反馈]: resolve 后自动 notify origin agent (agent-ceo) 关闭回路.
    """
    p = _decision_path(decision_id)
    if p is None or not p.exists():
        return False
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["status"] = "resolved"
        d["resolution"] = {
            "decision": (decision or "")[:500],
            "note": (note or "")[:500],
            "resolved_ts": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resolved_by": "user",
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        _notify_agent_decided(d, status="resolved")  # 回路通知, fail-safe
        return True
    except (OSError, ValueError):
        return False


def dismiss_decision(decision_id: str) -> bool:
    """[user ]: dismiss 不通知 agent-ceo (用户取消即可, 不需要 agent-ceo 知道).
    跟 resolve 不同: resolve = 决断结果应回路给 agent-ceo 推进; dismiss = 用户认为无需关心.
    """
    p = _decision_path(decision_id)
    if p is None or not p.exists():
        return False
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["status"] = "dismissed"
        d["resolution"] = {
            "decision": "dismissed",
            "note": "user dismissed without explicit resolution",
            "resolved_ts": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resolved_by": "user",
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        # dismiss 不通知 agent-ceo (user 直接拍)
        return True
    except (OSError, ValueError):
        return False


# ---------- 回路通知 (user 直接反馈) ----------

def _notify_agent_decided(decision: dict, status: str) -> bool:
    """[user ]: user 在 GUI 决断后, agent-ceo 应自动收到结果以 close loop.
    走 master /api/v1/agents/{agent_id}/send POST kind=chat. fail-safe 不阻 GUI 主路径.
    """
    agent_id = (decision.get("agent_id") or "").strip()
    if not agent_id:
        return False
    decision_id = decision.get("decision_id", "")
    summary = (decision.get("summary") or "")[:200]
    resolution = decision.get("resolution") or {}
    decided = (resolution.get("decision") or "")[:500]
    note = (resolution.get("note") or "")[:500]
    questions = decision.get("questions") or []
    # 抠原 question 题目供 agent-ceo 知道是哪个事
    q_titles = []
    for q in questions[:5]:
        if isinstance(q, dict):
            t = (q.get("question") or "")[:120]
            if t:
                q_titles.append(f"- {t}")
    q_block = "\n".join(q_titles) if q_titles else "(无题目)"
    text = (
        f"[user user_decisions {status}] decision_id={decision_id}\n"
        f"summary: {summary}\n"
        f"questions:\n{q_block}\n"
        f"---\n"
        f"user 决定: {decided}\n"
        f"note: {note}\n"
        f"resolved_ts: {resolution.get('resolved_ts', '')}\n"
        f"resolved_by: user\n"
        f"---\n"
        f"(自动回路通知, 来自 user_decisions resolve/dismiss API)"
    )
    body = {
        "kind": "chat",
        "payload": {
            "text": text,
            "decision_id": decision_id,
            "decision_status": status,
            "decision": decided,
            "note": note,
        },
        "from_agent": "user.default",
        "from_role": "user",
        "priority": 0,
    }
    master_url = os.environ.get("PRE_MASTER_URL", "http://127.0.0.1:19500")
    token = _resolve_token("hook")
    url = master_url.rstrip("/") + f"/api/v1/agents/{agent_id}/send"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }, method="POST")
        with _NO_PROXY_OPENER.open(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False  # fail-safe 不阻 GUI
