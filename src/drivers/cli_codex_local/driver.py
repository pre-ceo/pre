"""cli-codex-local driver — 接 Codex agent 到 pre bus.

发现规则: 扫 pre_rule/agents/{Users-user-cursor-xxx}/, 反推 cwd, 读
{cwd}/pre/agent_config.json, 只收 cli == "codex".
agent_id: <node_id>.cli-codex-local.<project_name>

detect_pending 内嵌 evaluator + auto allow/deny + audit log.
"""
from __future__ import annotations
import json
import os
import sys

# 复用现有 pre 模块
_PRE_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, os.path.join(_PRE_ROOT, "src"))

import hashlib
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional
from common.paths import PRE_LOG_ROOT, PRE_AGENT_HOME

from drivers.base import BaseDriver, AgentSpec
from tmux_helper import send_to_tmux, send_key, capture_pane

from .pending_parser import CodexPending, parse_codex_pending


# Codex TUI busy markers — 跟 Claude 完全不同 (没有 Simmering/esc to interrupt)
_BUSY_MARKERS = (
    "• Working",
    "• Loading",
    "Streaming",
    "Thinking…",
    "Thinking ",
    "Generating…",
)
# Codex idle 锚点 (回到 prompt + status 行)
_IDLE_MARKERS = (
    " tab to queue message",
    " context left",
    "› ",
)


def _is_pane_busy(pane: str) -> bool:
    """tail 10 行内是否有 busy marker (历史段不算)."""
    tail = "\n".join(pane.splitlines()[-10:])
    return any(m in tail for m in _BUSY_MARKERS)


def _has_idle_anchor(pane: str) -> bool:
    """tail 8 行内是否有 idle 锚点 (回到 prompt)."""
    tail = "\n".join(pane.splitlines()[-8:])
    return any(m in tail for m in _IDLE_MARKERS)


# 网络探测复用 Claude driver 的 cache (避免每个 cwd 都探一遍)
def _probe_network_cached(cwd: str) -> Optional[dict]:
    """复用 Claude driver 的 _probe_network_cached (同 cache, 30min TTL)."""
    try:
        from drivers.cli_claude_code_local.driver import _probe_network_cached as _claude_probe
        return _claude_probe(cwd)
    except (ImportError, Exception):
        return None


class CliCodexLocalDriver(BaseDriver):
    type_name = "cli-codex-local"

    async def init(self, node_ctx):
        await super().init(node_ctx)
        self.rule_root = os.environ.get(
            "PRE_RULE_ROOT",
            os.path.normpath(os.path.join(_PRE_ROOT, "..", "pre_rule")),
        )
        self.agents_dir = os.path.join(self.rule_root, "agents")
        # Codex agent 通常没 pre_rule/agents/<name>/ 目录 (因为它们不调 Claude hook),
        # 所以 discover 直接扫 cursor root. env 可覆盖.
        self.cursor_root = os.environ.get(
            "PRE_CURSOR_ROOT",
            PRE_AGENT_HOME,
        )
        self.node_id = node_ctx.get("node_id", "local")
        self.audit_dir = os.path.join(
            PRE_LOG_ROOT, "codex_driver"
        )
        # evaluator lazy import (避免 init 时拉满 pre 整套)
        self._evaluator = None

    async def discover_agents(self) -> list[AgentSpec]:
        """扫 cursor_root/*/pre/agent_config.json, 只收 cli == "codex".

        TODO: 待跟 claude driver 对齐, 改成扫 pre_rule/agents/<dir>/agent_pointer.json
        (用户决策: 配置以 cwd/pre 为准, 但 pre_rule/agents 仍作 driver 索引指针;
        不再扫 PRE_AGENT_HOME 兜底). 暂保留 cursor_root 扫描, 等 codex 这边初始化方法跟上.
        """
        out = []
        if not os.path.isdir(self.cursor_root):
            return out

        for name in sorted(os.listdir(self.cursor_root)):
            cwd = os.path.join(self.cursor_root, name)
            if not os.path.isdir(cwd):
                continue
            cfg_path = os.path.join(cwd, "pre", "agent_config.json")
            if not os.path.isfile(cfg_path):
                continue
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            cli_type = cfg.get("cli") or ""
            if cli_type != "codex":
                continue  # 非 Codex agent 不归本 driver 管

            project_name = cfg.get("project_name") or name
            tmux_session = cfg.get("tmux_session") or project_name
            mode = cfg.get("mode", "supervised")

            if cfg.get("role"):
                role = cfg["role"]
            elif mode in ("freerun", "autonomous"):
                role = "freerun-worker"
            else:
                role = "worker"

            cli_model = cfg.get("model") or None
            network = _probe_network_cached(cwd)

            agent_id = f"{self.node_id}.{self.type_name}.{project_name}"
            out.append(AgentSpec(
                agent_id=agent_id,
                role=role,
                capabilities=["text-chat", "tool-use"],
                metadata={
                    "cwd": cwd,
                    "tmux_session": tmux_session,
                    "mode": mode,
                    "project_name": project_name,
                    "cli": cli_type,
                    "cli_model": cli_model,
                    "network": network,
                    "auto_governor": cfg.get("auto_governor") or {},
                },
            ))
        return out

    async def send(self, agent_id: str, message: dict) -> bool:
        for spec in await self.discover_agents():
            if spec.agent_id == agent_id:
                ts = spec.metadata.get("tmux_session", "")
                if not ts:
                    return False
                payload = message.get("payload", {})
                text = (payload.get("text") or payload.get("prompt") or
                        json.dumps(payload, ensure_ascii=False))
                return send_to_tmux(ts, text)
        return False

    async def get_state(self, agent_id: str) -> str:
        """Codex 没原生 stop hook, 字段可能缺. 返 idle 兜底, 让 detect_activity
        覆盖真实状态."""
        for spec in await self.discover_agents():
            if spec.agent_id == agent_id:
                cwd = spec.metadata.get("cwd", "")
                name = cwd.lstrip("/").replace("/", "-")
                status_file = os.path.join(self.agents_dir, name, "stop_status.json")
                if os.path.isfile(status_file):
                    try:
                        with open(status_file) as f:
                            d = json.load(f)
                        return d.get("state", "idle")
                    except (json.JSONDecodeError, OSError):
                        pass
                return "idle"
        return "unknown"

    async def detect_pending(self, agent_id: str) -> Optional[dict]:
        """Codex pane → parse → evaluator → auto allow/deny / 上报 ask.
        默认 auto-decide 开启, 用户可在 agent_config.auto_governor.enabled=False 关闭.
        """
        for spec in await self.discover_agents():
            if spec.agent_id != agent_id:
                continue
            ts = spec.metadata.get("tmux_session", "")
            if not ts:
                return None
            pane = capture_pane(ts, lines=80)
            if not pane:
                return None
            pending = parse_codex_pending(pane, agent_id=agent_id)
            if pending is None:
                return None

            auto_cfg = spec.metadata.get("auto_governor") or {}
            auto_enabled = auto_cfg.get("enabled", True)

            if not auto_enabled:
                # 兼容路径: 仅上报, 不自动按键
                return {
                    "agent_id": agent_id,
                    "tool_kind": pending.tool_kind,
                    "description": pending.description[:200],
                    "since_pane_ts": time.time(),
                    "tmux_session": ts,
                    "prehook_decision": {
                        "decision": "ask",
                        "reason": "auto_governor disabled",
                        "source": "driver_passthrough",
                    },
                }

            # 主路径: 调 evaluator
            decision = self._evaluate(pending, spec)
            decision_name = str(decision.get("decision") or "ask")

            base_audit = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent_id": agent_id,
                "cwd": spec.metadata.get("cwd", ""),
                "tmux_session": ts,
                "tool_name": pending.tool_name,
                "tool_input_preview": _preview_tool_input(pending),
                "decision": decision_name,
                "reason": decision.get("reason", ""),
                "source": decision.get("source", ""),
            }

            if decision_name == "allow":
                ok = send_key(ts, pending.approve_key)
                base_audit["action"] = "approve_key_sent"
                base_audit["ok"] = ok
                self._audit(base_audit)
                return None

            if decision_name == "deny":
                ok = send_key(ts, pending.reject_key)
                base_audit["action"] = "reject_key_sent"
                base_audit["ok"] = ok
                self._audit(base_audit)
                return None

            # ask → 上报 master, 让 GUI/用户决定
            base_audit["action"] = "reported_to_user"
            base_audit["ok"] = True
            self._audit(base_audit)
            return {
                "agent_id": agent_id,
                "tool_kind": pending.tool_kind,
                "description": pending.description[:200],
                "since_pane_ts": time.time(),
                "tmux_session": ts,
                "prehook_decision": {
                    "decision": decision_name,
                    "reason": decision.get("reason", ""),
                    "source": decision.get("source", ""),
                },
            }
        return None

    def _evaluate(self, pending: CodexPending, spec: AgentSpec) -> dict:
        """调 pre evaluator. fail-closed: 任何异常 → ask."""
        if self._evaluator is None:
            try:
                from prehook_evaluator import evaluate_prehook
                self._evaluator = evaluate_prehook
            except Exception as e:
                return {"decision": "ask", "reason": f"evaluator import failed: {e}",
                        "source": "driver_fail_closed"}
        try:
            input_data = {
                "tool_name": pending.tool_name,
                "tool_input": pending.tool_input,
                "session_id": f"codex-{spec.metadata.get('project_name', 'unknown')}",
                "cwd": spec.metadata.get("cwd", ""),
                "transcript_path": "",
                "permission_mode": "default",
                "runtime": "codex",
                "agent_id": spec.agent_id,
            }
            return self._evaluator(input_data)
        except Exception as e:
            return {"decision": "ask", "reason": f"evaluator raised: {e}",
                    "source": "driver_fail_closed"}

    def _quick_decide(self, pending: CodexPending, spec: AgentSpec) -> str:
        """Fast-path 决策 — 仅跑 local rules + cache, 跳 governor (不阻塞 detect_activity).
        返 'allow' / 'deny' / 'ask' (含 unknown / cache miss).

        用途: detect_activity 高频 (10s) 调用, 不能跑 governor LLM. local + cache 命中即
        知道 driver 即将自动消化这个 pending, 不该让 master GUI 显示 blocked_user.
        miss → 视为 ask (保守, 等 governor 真判完).
        """
        try:
            from rules import evaluate as local_evaluate, GOVERNOR_NO_CACHE
            from cache import cache_key, get_cached
            from governor import ensure_agent_dir
            from config import load_config
        except Exception:
            return "ask"
        cwd = spec.metadata.get("cwd", "") or ""
        try:
            decision, _ = local_evaluate(pending.tool_name, pending.tool_input, cwd)
        except Exception:
            return "ask"
        if decision and decision != GOVERNOR_NO_CACHE:
            return decision  # local 直接判 allow/deny/ask
        # local 没决策 (None) 或 GOVERNOR_NO_CACHE → 看 cache
        try:
            cfg = load_config()
            agent_pre_dir = ensure_agent_dir(cfg.pre_base_dir, cwd)
            cached = get_cached(agent_pre_dir, cache_key(pending.tool_name, pending.tool_input), ttl=3600)
        except Exception:
            return "ask"
        if cached is not None:
            return cached[0]  # cache 命中过去 governor 决策
        return "ask"  # local 没决策 + cache miss → 真要 governor 跑, 标 blocked_user 让 user 看

    def _audit(self, entry: dict) -> None:
        """append-only jsonl, chmod 600. 失败不抛 (audit 不能阻断决策)."""
        try:
            os.makedirs(self.audit_dir, mode=0o700, exist_ok=True)
            date = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = os.path.join(self.audit_dir, f"auto_decision_{date}.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception:
            pass

    async def decide(self, agent_id: str, key: str) -> bool:
        for spec in await self.discover_agents():
            if spec.agent_id != agent_id:
                continue
            ts = spec.metadata.get("tmux_session", "")
            if not ts:
                return False
            return send_key(ts, key)
        return False

    async def detect_activity(self, agent_id: str) -> Optional[dict]:
        """Codex pane → state + recent_actions + last_response + pane_summary."""
        for spec in await self.discover_agents():
            if spec.agent_id != agent_id:
                continue
            ts = spec.metadata.get("tmux_session", "")
            if not ts:
                return None
            pane = capture_pane(ts, lines=200)
            if not pane:
                return None

            # state 判定: pending > busy > idle.
            # codex 的 idle 锚点 (`tab to queue message` / `context left` /
            # `›` prompt) 在 busy 时也显示 (底部状态栏常驻). 只要 busy marker (`• Working`
            # / `Thinking…` 等) 出现就 busy, 不能被 idle 锚点覆盖.
            #
            # pending 出现时不要立即标 blocked_user, 先用 fast-path
            # (local rules + cache, 跳 governor 不阻塞) 看能否自动决策. local allow/deny 或
            # cache hit 明确决策 → state=busy (driver detect_pending 下一秒会自动按键).
            # 仅 local 没决策 + cache 也没命中 (即真要 governor / user 判) 才标 blocked_user.
            # 避免 evaluator allow/deny 路径 (一闪而过) 让 master GUI 错亮 blocked_user 提醒.
            pending = parse_codex_pending(pane, agent_id=agent_id)
            has_busy = _is_pane_busy(pane)
            if pending:
                quick = self._quick_decide(pending, spec)
                if quick in ("allow", "deny"):
                    state = "busy"  # driver 即将自动按键消化, 不打扰 user
                else:
                    state = "blocked_user"  # 真要 ask user
            elif has_busy:
                state = "busy"
            else:
                state = "idle"
            has_pending = pending is not None

            last_action = pending.description if pending else _extract_last_codex_action(pane)
            tool_kind = pending.tool_kind if pending else "unknown"
            recent_actions = _extract_recent_codex_actions(pane, n=5)
            last_response_excerpt = _extract_last_codex_response(pane)

            tail_lines = [l for l in pane.splitlines() if l.strip()][-30:]
            pane_summary = "\n".join(tail_lines)[:2000]
            pane_fp = hashlib.sha1(pane_summary.encode("utf-8")).hexdigest()

            return {
                "agent_id": agent_id,
                "state": state,
                "last_action": (last_action or "")[:200],
                "tool_kind": tool_kind,
                "recent_actions": recent_actions,
                "last_response_excerpt": last_response_excerpt,
                "claude_status": None,  # codex 无对应字段
                "pane_summary": pane_summary,
                "pane_fp": pane_fp,
                "tmux_session": ts,
                "since_ts": time.time(),
            }
        return None

    async def shutdown(self):
        pass


# Codex pane 辅助抽取 (跟 Claude 不同 — Codex 没 ⏺/⎿ 标记, 用 reasoning/cmd 起首行)

# 常见 Codex 操作行起首 (实测后可补)
_CODEX_ACTION_RE = re.compile(
    r"^(?:•\s+)?(?:Running|Bash|Edit|Write|Read|Search|Apply|Patching)\b(.*)$"
)


def _extract_recent_codex_actions(pane: str, n: int = 5) -> list[dict]:
    """倒序抽 Codex 操作行 (实测后再调). 无 marker 时返空."""
    lines = pane.splitlines()
    actions = []
    for i in range(len(lines) - 1, -1, -1):
        if len(actions) >= n:
            break
        line = lines[i].strip()
        m = _CODEX_ACTION_RE.match(line)
        if m:
            head = line.split(maxsplit=1)
            tool = head[0] if head[0] != "•" else (head[1].split(maxsplit=1)[0] if len(head) > 1 else "")
            actions.append({"tool": tool, "summary": line[:80]})
    return actions


def _extract_last_codex_action(pane: str) -> str:
    """tail 取最近一行非空非提示行作为 last_action 兜底."""
    lines = pane.splitlines()
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("›") or "tab to queue" in s.lower() or "context left" in s.lower():
            continue
        return s[:200]
    return ""


def _extract_last_codex_response(pane: str) -> str:
    """末尾连续非工具/非提示行 = agent 最后输出. 截 500 字."""
    lines = pane.splitlines()
    collected = []
    for line in reversed(lines):
        s = line.rstrip()
        if not s.strip():
            if collected:
                continue
            continue
        first = s.lstrip()[:3]
        if (first.startswith("›") or first.startswith("•") or
                "──" in s or "━━" in s or
                "tab to queue" in s.lower() or
                "context left" in s.lower() or
                "esc to" in s.lower()):
            if collected:
                break
            continue
        collected.append(s.strip())
    if not collected:
        return ""
    text = "\n".join(reversed(collected))
    if len(text) > 500:
        text = text[:500] + "..."
    return text


def _preview_tool_input(pending: CodexPending) -> str:
    if pending.tool_name == "Bash":
        return str(pending.tool_input.get("command", ""))[:240]
    if pending.tool_name in ("Read", "Write", "Edit"):
        return str(pending.tool_input.get("file_path", ""))[:240]
    return json.dumps(pending.tool_input, ensure_ascii=False)[:240]
