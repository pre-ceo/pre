"""cli-gemini-local driver — 接 Gemini CLI agent 到 pre bus.

发现规则: 扫 pre_rule/agents/<dir>/agent_pointer.json, pointer.cli == "gemini".
agent_id: <node_id>.cli-gemini-local.<project_name>

**架构跟 codex driver 对齐, 不走 hook 路径**:
  - Gemini 原生有 hook (BeforeTool/AfterAgent), 但 hook decision schema 只支持
    allow/deny 二态, 没有 ask. 真要实现 ask 必须 driver pane scrape.
  - 既然 ask 必须走 pane scrape, 干脆 allow/deny 也走同一路径, 整套设计跟 codex
    一致, 简化 mental model.
  - Gemini 跑在 default approval-mode 下, 工具调用会弹原生 approval UI
    ("Allow execution of [<tool>]?"), driver detect_pending 抓这个 UI →
    内嵌 evaluator 决策 → 自动注 "1" allow / "Esc" reject / 上报 ask 给 user.
"""
from __future__ import annotations
import json
import os
import sys

_PRE_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, os.path.join(_PRE_ROOT, "src"))

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Optional
from common.paths import PRE_LOG_ROOT

from drivers.base import BaseDriver, AgentSpec, InitResult
from tmux_helper import send_to_tmux, send_key, capture_pane, has_session

from .pending_parser import GeminiPending, parse_gemini_pending


# Gemini TUI markers (实测 gemini-cli v0.42 idle/busy pane).
# Busy 状态候选 spinner / "Thinking" 待 fixture 校准.
_BUSY_MARKERS = (
    "Loading",
    "Thinking",
    "Generating",
    "Running",
    "⠋",  # spinner chars (待 fixture 验证)
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
)
# Gemini idle 锚点 (回到 prompt) — 实测 v0.42 idle pane.
# 强锚点: " > Type your message or @path/to/file" 是 idle input box hint.
# "? for shortcuts" + "Shift+Tab to accept edits" 是 idle footer.
_IDLE_MARKERS = (
    "Type your message",
    "? for shortcuts",
    "Shift+Tab to accept edits",
)
# Gemini chat marker: 用户消息 " > ..." (history block 中);
# gemini 回应前缀 "✦" (U+2726).
_CHAT_USER_PREFIX = " > "
_CHAT_BOT_PREFIX = "✦"


def _is_pane_busy(pane: str) -> bool:
    """tail 10 行内是否有 busy marker (历史段不算)."""
    tail = "\n".join(pane.splitlines()[-10:])
    return any(m in tail for m in _BUSY_MARKERS)


def _has_idle_anchor(pane: str) -> bool:
    """tail 8 行内是否有 idle 锚点."""
    tail = "\n".join(pane.splitlines()[-8:])
    return any(m in tail for m in _IDLE_MARKERS)


def _probe_network_cached(cwd: str) -> Optional[dict]:
    """复用 Claude driver 的 _probe_network_cached (同 cache, 30min TTL)."""
    try:
        from drivers.cli_claude_code_local.driver import _probe_network_cached as _claude_probe
        return _claude_probe(cwd)
    except (ImportError, Exception):
        return None




def _extract_gemini_llm_route(cwd: str) -> Optional[dict]:
    """读 ~/.gemini/settings.json 抽 gemini 配置.

    Gemini settings.json (实测):
      { "security": { "auth": { "selectedType": "oauth-personal" } },
        "ui": { "theme": "..." } }

    auth.selectedType 可能值:
      - "oauth-personal" (Google account OAuth)
      - "vertex-ai" (Cloud Vertex AI key)
      - "gemini-api-key" (Generative Language API key)
      - "ml-dev-server"

    scope=global (gemini settings 是全局的; 也支持 cwd/.gemini/settings.json
    项目覆盖, 但本函数先只读全局, fixture 验证后再扩 dual-source).

    返回字段:
      - auth_type: str | None
      - has_oauth: bool (selectedType=oauth-personal)
      - has_api_key: bool (gemini-api-key / 环境 GEMINI_API_KEY/GOOGLE_API_KEY)
      - source: list[str]
      - scope: "global"
    """
    config_path = os.path.expanduser("~/.gemini/settings.json")
    if not os.path.isfile(config_path):
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return {
                "auth_type": None,
                "has_oauth": False,
                "has_api_key": True,
                "source": ["env:GEMINI_API_KEY/GOOGLE_API_KEY"],
                "scope": "global",
            }
        return None

    try:
        with open(config_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    auth = (doc.get("security") or {}).get("auth") or {}
    auth_type = auth.get("selectedType") or None

    has_oauth = auth_type == "oauth-personal"
    has_api_key = auth_type == "gemini-api-key" or bool(
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    )

    if not (auth_type or has_oauth or has_api_key):
        return None

    return {
        "auth_type": auth_type,
        "has_oauth": has_oauth,
        "has_api_key": has_api_key,
        "source": [config_path],
        "scope": "global",
    }


class CliGeminiLocalDriver(BaseDriver):
    type_name = "cli-gemini-local"
    cli_name = "gemini"

    async def init(self, node_ctx):
        await super().init(node_ctx)
        self.rule_root = os.environ.get(
            "PRE_RULE_ROOT",
            os.path.normpath(os.path.join(_PRE_ROOT, "..", "pre_rule")),
        )
        self.agents_dir = os.path.join(self.rule_root, "agents")
        self.node_id = node_ctx.get("node_id", "local")
        self.audit_dir = os.path.join(
            PRE_LOG_ROOT, "gemini_driver"
        )
        self._evaluator = None

    async def discover_agents(self) -> list[AgentSpec]:
        """扫 pre_rule/agents/<dir>/agent_pointer.json. pointer.cli == "gemini" 的归本 driver."""
        out: list[AgentSpec] = []
        if not os.path.isdir(self.agents_dir):
            return out

        for name in sorted(os.listdir(self.agents_dir)):
            agent_pre_dir = os.path.join(self.agents_dir, name)
            if not os.path.isdir(agent_pre_dir):
                continue
            spec = self._discover_one(name, agent_pre_dir)
            if spec is not None:
                out.append(spec)
        return out

    def _discover_one(self, name: str, agent_pre_dir: str) -> Optional[AgentSpec]:
        pointer_path = os.path.join(agent_pre_dir, "agent_pointer.json")
        if not os.path.isfile(pointer_path):
            return None

        try:
            with open(pointer_path) as f:
                pointer = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

        if (pointer.get("cli") or "claude") != "gemini":
            return None

        cwd = pointer.get("cwd", "")
        if not cwd or not os.path.isabs(cwd):
            return self._failed_spec(
                f"badpointer-{name}",
                reason="pointer-no-cwd",
                hint=f"pointer missing absolute 'cwd': {pointer_path}",
                extra={"pre_rule_dir": agent_pre_dir},
            )

        project_name = (pointer.get("project_name")
                        or os.path.basename(cwd.rstrip("/")) or name)
        agent_id = f"{self.node_id}.{self.type_name}.{project_name}"
        common_extra = {
            "cwd": cwd,
            "project_name": project_name,
            "pre_rule_dir": agent_pre_dir,
        }

        if not os.path.isdir(cwd):
            return self._failed_spec(
                project_name,
                reason="cwd-missing",
                hint=f"cwd not found on disk: {cwd}",
                extra=common_extra,
            )

        cfg_path = os.path.join(cwd, "pre", "agent_config.json")
        if not os.path.isfile(cfg_path):
            return self._failed_spec(
                project_name,
                reason="not-initialized",
                hint=f"missing {cfg_path}; run pre-init --driver gemini in {cwd}",
                extra=common_extra,
            )
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return self._failed_spec(
                project_name,
                reason="invalid-agent-config",
                hint=f"corrupt {cfg_path}: {e}",
                extra=common_extra,
            )

        cfg_cli = cfg.get("cli") or "claude"
        if cfg_cli != "gemini":
            return self._failed_spec(
                project_name,
                reason="cli-mismatch",
                hint=f"pointer cli=gemini but {cfg_path} cli={cfg_cli}",
                extra=common_extra,
            )

        tmux_session = cfg.get("tmux_session") or project_name
        mode = cfg.get("mode", "supervised")
        runtime_extra = dict(common_extra)
        runtime_extra["tmux_session"] = tmux_session
        runtime_extra["mode"] = mode

        if not has_session(tmux_session, timeout=2.0):
            return self._failed_spec(
                project_name,
                reason="tmux-session-missing",
                hint=(f"tmux session '{tmux_session}' not running; "
                      f"spawn via scripts/spawn_agent.sh {agent_id}"),
                extra=runtime_extra,
            )

        if cfg.get("role"):
            role = cfg["role"]
        elif mode in ("freerun", "autonomous"):
            role = "freerun-worker"
        else:
            role = "worker"

        cli_model = cfg.get("model") or None
        network = _probe_network_cached(cwd)
        llm_route = _extract_gemini_llm_route(cwd)

        return AgentSpec(
            agent_id=agent_id,
            role=role,
            capabilities=["text-chat", "tool-use"],
            metadata={
                "status": "ok",
                "cwd": cwd,
                "tmux_session": tmux_session,
                "mode": mode,
                "project_name": project_name,
                "cli": "gemini",
                "cli_model": cli_model,
                "llm_route": llm_route,
                "network": network,
                "auto_governor": cfg.get("auto_governor") or {},
            },
        )

    def _failed_spec(self, slug: str, *, reason: str, hint: str,
                     extra: Optional[dict] = None) -> AgentSpec:
        meta: dict = {
            "status": "failed",
            "failure_reason": reason,
            "failure_hint": hint,
            "cli": "gemini",
        }
        if extra:
            meta.update(extra)
        return AgentSpec(
            agent_id=f"{self.node_id}.{self.type_name}.{slug}",
            role="failed",
            capabilities=[],
            metadata=meta,
        )

    @staticmethod
    def _is_active(spec: AgentSpec) -> bool:
        return spec.metadata.get("status") == "ok"

    async def send(self, agent_id: str, message: dict) -> bool:
        for spec in await self.discover_agents():
            if spec.agent_id == agent_id:
                if not self._is_active(spec):
                    print(f"[driver] send rejected: agent {agent_id} status=failed "
                          f"reason={spec.metadata.get('failure_reason')}", flush=True)
                    return False
                ts = spec.metadata.get("tmux_session", "")
                if not ts:
                    return False
                payload = message.get("payload", {})
                text = (payload.get("text") or payload.get("prompt") or
                        json.dumps(payload, ensure_ascii=False))
                return send_to_tmux(ts, text)
        return False

    async def get_state(self, agent_id: str) -> str:
        """Gemini 没原生 stop hook (driver 内嵌 evaluator 路径). 返 idle 兜底,
        让 detect_activity 覆盖真实状态."""
        for spec in await self.discover_agents():
            if spec.agent_id == agent_id:
                if not self._is_active(spec):
                    return "failed"
                pre_rule_dir = spec.metadata.get("pre_rule_dir", "")
                status_file = os.path.join(pre_rule_dir, "stop_status.json") if pre_rule_dir else ""
                if status_file and os.path.isfile(status_file):
                    try:
                        with open(status_file) as f:
                            d = json.load(f)
                        return d.get("state", "idle")
                    except (json.JSONDecodeError, OSError):
                        pass
                return "idle"
        return "unknown"

    async def detect_pending(self, agent_id: str) -> Optional[dict]:
        """Gemini pane → parse → evaluator → auto allow/deny / 上报 ask."""
        for spec in await self.discover_agents():
            if spec.agent_id != agent_id:
                continue
            if not self._is_active(spec):
                return None
            ts = spec.metadata.get("tmux_session", "")
            if not ts:
                return None
            pane = capture_pane(ts, lines=80)
            if not pane:
                return None
            pending = parse_gemini_pending(pane, agent_id=agent_id)
            if pending is None:
                return None

            auto_cfg = spec.metadata.get("auto_governor") or {}
            auto_enabled = auto_cfg.get("enabled", True)

            if not auto_enabled:
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

    def _evaluate(self, pending: GeminiPending, spec: AgentSpec) -> dict:
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
                "session_id": f"gemini-{spec.metadata.get('project_name', 'unknown')}",
                "cwd": spec.metadata.get("cwd", ""),
                "transcript_path": "",
                "permission_mode": "default",
                "runtime": "gemini",
                "agent_id": spec.agent_id,
            }
            return self._evaluator(input_data)
        except Exception as e:
            return {"decision": "ask", "reason": f"evaluator raised: {e}",
                    "source": "driver_fail_closed"}

    def _quick_decide(self, pending: GeminiPending, spec: AgentSpec) -> str:
        """Fast-path 决策 (local rules + cache 跳 governor). 不阻塞 detect_activity."""
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
            return decision
        try:
            cfg = load_config()
            agent_pre_dir = ensure_agent_dir(cfg.pre_base_dir, cwd)
            cached = get_cached(agent_pre_dir, cache_key(pending.tool_name, pending.tool_input), ttl=3600)
        except Exception:
            return "ask"
        if cached is not None:
            return cached[0]
        return "ask"

    def _audit(self, entry: dict) -> None:
        """append-only jsonl, chmod 600."""
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
            if not self._is_active(spec):
                print(f"[driver] decide rejected: agent {agent_id} status=failed "
                      f"reason={spec.metadata.get('failure_reason')}", flush=True)
                return False
            ts = spec.metadata.get("tmux_session", "")
            if not ts:
                return False
            return send_key(ts, key)
        return False

    async def detect_activity(self, agent_id: str) -> Optional[dict]:
        """Gemini pane → state + recent_actions + last_response + pane_summary."""
        for spec in await self.discover_agents():
            if spec.agent_id != agent_id:
                continue
            if not self._is_active(spec):
                return {
                    "agent_id": agent_id,
                    "state": "failed",
                    "failure_reason": spec.metadata.get("failure_reason"),
                    "failure_hint": spec.metadata.get("failure_hint"),
                    "since_ts": time.time(),
                }
            ts = spec.metadata.get("tmux_session", "")
            if not ts:
                return None
            pane = capture_pane(ts, lines=200)
            if not pane:
                return None

            pending = parse_gemini_pending(pane, agent_id=agent_id)
            has_busy = _is_pane_busy(pane)
            if pending:
                quick = self._quick_decide(pending, spec)
                if quick in ("allow", "deny"):
                    state = "busy"
                else:
                    state = "blocked_user"
            elif has_busy:
                state = "busy"
            else:
                state = "idle"

            last_action = pending.description if pending else _extract_last_gemini_action(pane)
            tool_kind = pending.tool_kind if pending else "unknown"
            recent_actions = _extract_recent_gemini_actions(pane, n=5)
            last_response_excerpt = _extract_last_gemini_response(pane)

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
                "claude_status": None,
                "pane_summary": pane_summary,
                "pane_fp": pane_fp,
                "tmux_session": ts,
                "since_ts": time.time(),
            }
        return None

    async def init_agent(self, target_dir: str, opts: Optional[dict] = None) -> InitResult:
        """幂等初始化一个 gemini agent.

        4 步 (跟 codex 4 步对应, 不写 .gemini/settings.json hook — gemini 走
        driver 内嵌 evaluator + pane scrape, 跟 codex 一致):
          1. validate target_dir
          2. cwd/pre/ 树 + agent_config.json (cli=gemini, start_command=gemini)
          3. pre_rule/agents/<dir>/agent_pointer.json (cli=gemini)
          4. tmux session check

        opts: mode, tmux_session, project_name, model, role, write_templates.
        """
        opts = opts or {}
        result = InitResult(ok=False, agent_id="", target_dir=target_dir)

        if not os.path.isabs(target_dir):
            result.failures.append(f"target_dir must be absolute: {target_dir}")
            return result
        if not os.path.isdir(target_dir):
            result.failures.append(f"target_dir does not exist: {target_dir}")
            return result

        project_name = (opts.get("project_name")
                        or os.path.basename(target_dir.rstrip("/")) or "agent")
        tmux_session = opts.get("tmux_session") or project_name
        mode = opts.get("mode") or "supervised"
        agent_id = f"{self.node_id}.{self.type_name}.{project_name}"
        result.agent_id = agent_id

        pre_dir = os.path.join(target_dir, "pre")
        for sub in (pre_dir,
                    os.path.join(pre_dir, "findings"),
                    os.path.join(pre_dir, "findings", "processed"),
                    os.path.join(pre_dir, "reports")):
            if not os.path.isdir(sub):
                try:
                    os.makedirs(sub, exist_ok=True)
                    result.created.append(sub)
                except OSError as e:
                    result.failures.append(f"mkdir {sub}: {e}")
                    return result

        cfg_path = os.path.join(pre_dir, "agent_config.json")
        existing: dict = {}
        existing_present = os.path.isfile(cfg_path)
        if existing_present:
            try:
                with open(cfg_path) as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        existing = loaded
            except (OSError, json.JSONDecodeError) as e:
                result.failures.append(f"existing {cfg_path} unreadable: {e}")
                return result
            existing_cli = existing.get("cli")
            if existing_cli and existing_cli != "gemini":
                result.conflicts.append(
                    f"{cfg_path} cli={existing_cli}; refuse to overwrite "
                    f"(this cwd belongs to a non-gemini driver)"
                )
                return result

        # preserve 用户字段, force driver-owned 字段. mcp.server 强 normalize "pre"
        # 防止 stale "fn_pre" 等老值保留.
        cfg = dict(existing)
        cfg["cli"] = "gemini"
        cfg["driver_type"] = self.type_name
        cfg.setdefault("mode", mode)
        cfg.setdefault("tmux_session", tmux_session)
        cfg.setdefault("project_name", project_name)
        cfg.setdefault("start_command", "gemini")
        if opts.get("model"):
            cfg["model"] = opts["model"]
        if opts.get("role"):
            cfg["role"] = opts["role"]
        mcp_block = cfg.get("mcp")
        if not isinstance(mcp_block, dict):
            mcp_block = {}
        mcp_block["server"] = "pre"
        mcp_block["caller_agent_id"] = agent_id
        cfg["mcp"] = mcp_block

        if existing_present and cfg == existing:
            result.skipped.append(cfg_path)
        else:
            try:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                if existing_present:
                    result.created.append(f"{cfg_path} (normalized)")
                else:
                    result.created.append(cfg_path)
            except OSError as e:
                result.failures.append(f"write {cfg_path}: {e}")
                return result

        if opts.get("write_templates", True):
            rules_path = os.path.join(pre_dir, "rules.md")
            if not os.path.isfile(rules_path):
                with open(rules_path, "w", encoding="utf-8") as f:
                    f.write(
                        f"# {project_name} — PreToolUse Rules (gemini)\n\n"
                        f"Driver 内嵌 evaluator 评估 Gemini approval, 叠加此规则到"
                        f"全局规则之上.\n\n"
                        f"## 额外 ALLOW\n\n## 额外 ASK\n\n## 额外 DENY\n"
                    )
                result.created.append(rules_path)
            else:
                result.skipped.append(rules_path)

            next_path = os.path.join(pre_dir, "next.md")
            if not os.path.isfile(next_path):
                with open(next_path, "w", encoding="utf-8") as f:
                    f.write(
                        f"# {project_name} — Next Task\n\n"
                        f"autonomous/freerun 模式时引导 agent 读此文件决定下一步.\n"
                    )
                result.created.append(next_path)
            else:
                result.skipped.append(next_path)

        # 3. pre_rule/agents/<dir>/agent_pointer.json
        dir_name = target_dir.strip("/").replace("/", "-")
        agent_pre_dir = os.path.join(self.agents_dir, dir_name)
        if not os.path.isdir(agent_pre_dir):
            try:
                os.makedirs(agent_pre_dir, exist_ok=True)
                result.created.append(agent_pre_dir)
            except OSError as e:
                result.failures.append(f"mkdir {agent_pre_dir}: {e}")
                return result

        pointer_path = os.path.join(agent_pre_dir, "agent_pointer.json")
        if os.path.isfile(pointer_path):
            try:
                with open(pointer_path) as f:
                    existing_ptr = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                result.conflicts.append(
                    f"{pointer_path} unreadable ({e}); remove and re-run pre-init"
                )
            else:
                if existing_ptr.get("cwd") != target_dir:
                    result.conflicts.append(
                        f"{pointer_path} cwd={existing_ptr.get('cwd')!r} "
                        f"but target_dir={target_dir!r}; remove pointer to re-init"
                    )
                elif (existing_ptr.get("cli") or "claude") != "gemini":
                    result.conflicts.append(
                        f"{pointer_path} cli={existing_ptr.get('cli')!r} but expected gemini"
                    )
                else:
                    result.skipped.append(pointer_path)
        else:
            pointer = {
                "cwd": target_dir,
                "agent_id": agent_id,
                "cli": "gemini",
                "project_name": project_name,
                "created_at": time.time(),
            }
            try:
                with open(pointer_path, "w", encoding="utf-8") as f:
                    json.dump(pointer, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                result.created.append(pointer_path)
            except OSError as e:
                result.failures.append(f"write {pointer_path}: {e}")
                return result

        # 4. tmux session 检查
        spawn_script = os.path.join(_PRE_ROOT, "scripts", "spawn_agent.sh")
        if has_session(tmux_session, timeout=2.0):
            result.skipped.append(f"tmux session '{tmux_session}' already running")
        else:
            result.next_steps.append(
                f"tmux session '{tmux_session}' not running. To spawn gemini:\n"
                f"  bash {spawn_script} {agent_id}"
            )

        result.ok = (not result.failures
                     and not result.conflicts
                     and has_session(tmux_session, timeout=1.0))
        return result

    async def shutdown(self):
        pass


# Gemini pane 辅助抽取 (待 fixture 实测后调). 第一版用通用 line pattern.

_GEMINI_ACTION_RE = re.compile(
    r"^(?:•\s+)?(?:Running|Bash|Edit|Write|Read|Search|Patching|Tool|Apply)\b(.*)$"
)


def _extract_recent_gemini_actions(pane: str, n: int = 5) -> list[dict]:
    """倒序抽 Gemini 操作行 (实测后再调)."""
    lines = pane.splitlines()
    actions = []
    for i in range(len(lines) - 1, -1, -1):
        if len(actions) >= n:
            break
        line = lines[i].strip()
        m = _GEMINI_ACTION_RE.match(line)
        if m:
            head = line.split(maxsplit=1)
            tool = head[0] if head[0] != "•" else (head[1].split(maxsplit=1)[0] if len(head) > 1 else "")
            actions.append({"tool": tool, "summary": line[:80]})
    return actions


def _extract_last_gemini_action(pane: str) -> str:
    lines = pane.splitlines()
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if (s.startswith(">") or "type your message" in s.lower()
                or "context left" in s.lower()):
            continue
        return s[:200]
    return ""


def _extract_last_gemini_response(pane: str) -> str:
    """抽 gemini 末尾的纯文字回应. gemini response 前缀 `✦`, 跳过
    用户消息 (` > ...`) / box drawing (▄▄/▀▀) / idle hint.
    """
    lines = pane.splitlines()
    collected = []
    for line in reversed(lines):
        s = line.rstrip()
        if not s.strip():
            if collected:
                continue
            continue
        # 跳过 box drawing
        if "▄" in s or "▀" in s or "──" in s or "━━" in s:
            if collected:
                break
            continue
        # 跳过用户消息行
        if s.lstrip().startswith("> ") or s.lstrip().startswith("›"):
            if collected:
                break
            continue
        # 跳过 idle hint / footer (gemini footer 含 "workspace" / "sandbox" /
        # "/model" header row + value row "~/cursor/... no sandbox ... % used")
        lower = s.lower()
        if ("type your message" in lower or
                "? for shortcuts" in lower or
                "shift+tab to accept" in lower or
                "workspace (/" in lower or
                "no sandbox" in lower or
                "% used" in lower or
                ("auto (" in lower and ")" in lower)):
            if collected:
                break
            continue
        # 去掉 gemini response 前缀 ✦
        s_clean = s.lstrip()
        if s_clean.startswith("✦"):
            s_clean = s_clean[1:].lstrip()
        collected.append(s_clean.strip())
    if not collected:
        return ""
    text = "\n".join(reversed(collected))
    if len(text) > 500:
        text = text[:500] + "..."
    return text


def _preview_tool_input(pending: GeminiPending) -> str:
    if pending.tool_name == "Bash":
        return str(pending.tool_input.get("command", ""))[:240]
    if pending.tool_name in ("Read", "Write", "Edit"):
        return str(pending.tool_input.get("file_path", ""))[:240]
    return json.dumps(pending.tool_input, ensure_ascii=False)[:240]
