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

from drivers.base import BaseDriver, AgentSpec, InitResult
from tmux_helper import send_to_tmux, send_key, capture_pane, has_session

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


def _extract_codex_llm_route(cwd: str) -> Optional[dict]:
    """读 ~/.codex/config.toml + ~/.codex/auth.json 抽 codex 鉴权 / 路由.

    codex 配置是**全局**的 (没有目录覆盖语义), 所以 scope=global. 跟 claude 的
    目录级 `.claude/settings.json` 区分开.

    返回字段 (任一非默认才返 dict, 全默认返 None):
      - model: str | None (e.g. "gpt-5.5"); 显式配置才有, 用 default 不写时为 None
      - model_provider: str | None (e.g. "openai")
      - base_url: str | None ([model_providers.<provider>] 块内的 base_url)
      - has_api_key: bool (config.toml provider 块有 api_key 字段, 或 OPENAI_API_KEY env)
      - has_oauth: bool (~/.codex/auth.json 存在 — ChatGPT 账号登录)
      - source: list[str]
      - scope: "global" (固定)

    敏感值不入 metadata, 只标 has_*=True. ChatGPT OAuth 是 codex 默认推荐方式
    (运行时 pane footer 显示 model + cwd, 但 fs 上不写 explicit model).
    """
    config_path = os.path.expanduser("~/.codex/config.toml")
    auth_path = os.path.expanduser("~/.codex/auth.json")
    has_oauth = os.path.isfile(auth_path)

    if not os.path.isfile(config_path):
        # 没 config 但可能有 auth (OAuth-only) 或 env API key
        has_api_key = bool(os.environ.get("OPENAI_API_KEY"))
        if has_oauth or has_api_key:
            sources = []
            if has_oauth:
                sources.append(auth_path)
            if has_api_key:
                sources.append("env:OPENAI_API_KEY")
            return {
                "model": None,
                "model_provider": None,
                "base_url": None,
                "has_api_key": has_api_key,
                "has_oauth": has_oauth,
                "source": sources,
                "scope": "global",
            }
        return None

    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return None

    try:
        with open(config_path, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        # config.toml 损坏不阻塞: 仍按 OAuth-only 返
        if has_oauth:
            return {
                "model": None, "model_provider": None, "base_url": None,
                "has_api_key": False, "has_oauth": True,
                "source": [auth_path], "scope": "global",
            }
        return None

    model = doc.get("model") or None
    model_provider = doc.get("model_provider") or None
    base_url = None
    has_api_key = False
    if model_provider:
        providers = doc.get("model_providers") or {}
        prov = providers.get(model_provider) if isinstance(providers, dict) else None
        if isinstance(prov, dict):
            base_url = prov.get("base_url") or None
            if prov.get("api_key"):
                has_api_key = True
    if not has_api_key and os.environ.get("OPENAI_API_KEY"):
        has_api_key = True

    if not (model or model_provider or base_url or has_api_key or has_oauth):
        return None

    sources = [config_path]
    if has_oauth:
        sources.append(auth_path)

    return {
        "model": model,
        "model_provider": model_provider,
        "base_url": base_url,
        "has_api_key": has_api_key,
        "has_oauth": has_oauth,
        "source": sources,
        "scope": "global",
    }


class CliCodexLocalDriver(BaseDriver):
    type_name = "cli-codex-local"
    cli_name = "codex"

    async def init(self, node_ctx):
        await super().init(node_ctx)
        self.rule_root = os.environ.get(
            "PRE_RULE_ROOT",
            os.path.normpath(os.path.join(_PRE_ROOT, "..", "pre_rule")),
        )
        self.agents_dir = os.path.join(self.rule_root, "agents")
        self.node_id = node_ctx.get("node_id", "local")
        self.audit_dir = os.path.join(
            PRE_LOG_ROOT, "codex_driver"
        )
        # evaluator lazy import (避免 init 时拉满 pre 整套)
        self._evaluator = None

    async def discover_agents(self) -> list[AgentSpec]:
        """扫 pre_rule/agents/<dir>/agent_pointer.json. pointer.cli == "codex" 的归本 driver.
        跟 claude driver 同模式 (SOT 统一在 pointer + cwd/pre/agent_config.json).
        配置缺一项 → status=failed 仍 yield, GUI 看见原因.
        """
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
        """单 pre_rule/agents/<name>/ → AgentSpec, 或 None (非 codex 归别的 driver)."""
        pointer_path = os.path.join(agent_pre_dir, "agent_pointer.json")
        if not os.path.isfile(pointer_path):
            # 没 pointer 完全跳过 (claude driver 会自己 yield orphan)
            return None

        try:
            with open(pointer_path) as f:
                pointer = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None  # 让 claude driver 报 invalid-pointer; 此 driver 不抢

        if (pointer.get("cli") or "claude") != "codex":
            return None  # 非 codex agent 跳过

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
                hint=f"missing {cfg_path}; run pre-init --driver codex in {cwd}",
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
        if cfg_cli != "codex":
            return self._failed_spec(
                project_name,
                reason="cli-mismatch",
                hint=f"pointer cli=codex but {cfg_path} cli={cfg_cli}",
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
        llm_route = _extract_codex_llm_route(cwd)

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
                "cli": "codex",
                "cli_model": cli_model,
                "llm_route": llm_route,
                "network": network,
                "auto_governor": cfg.get("auto_governor") or {},
            },
        )

    def _failed_spec(self, slug: str, *, reason: str, hint: str,
                     extra: Optional[dict] = None) -> AgentSpec:
        """构造 status=failed 的 AgentSpec. slug 决定 agent_id 末段."""
        meta: dict = {
            "status": "failed",
            "failure_reason": reason,
            "failure_hint": hint,
            "cli": "codex",
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
        """Codex 没原生 stop hook, 字段可能缺. 返 idle 兜底, 让 detect_activity
        覆盖真实状态. failed agent 返 "failed"."""
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
        """Codex pane → parse → evaluator → auto allow/deny / 上报 ask.
        默认 auto-decide 开启, 用户可在 agent_config.auto_governor.enabled=False 关闭.
        failed agent 拒绝交互.
        """
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
        """Codex pane → state + recent_actions + last_response + pane_summary.
        failed agent 返带 failure 信息的精简 dict (GUI 渲染用)."""
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

    async def init_agent(self, target_dir: str, opts: Optional[dict] = None) -> InitResult:
        """幂等初始化一个 codex agent.

        4 步 (跟 claude 5 步对应, 砍 .claude/settings.json hook — codex 无 hook 接口,
        approval 走 driver 内嵌 evaluator):
          1. validate target_dir
          2. cwd/pre/ 树 + agent_config.json (cli=codex, start_command=codex)
          3. pre_rule/agents/<dir>/agent_pointer.json (cli=codex)
          4. tmux session check

        opts: mode, tmux_session, project_name, model, role, write_templates.
        """
        opts = opts or {}
        result = InitResult(ok=False, agent_id="", target_dir=target_dir)

        # 1. validate
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

        # 2. target_dir/pre/ 树 + agent_config.json
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
            if existing_cli and existing_cli != "codex":
                result.conflicts.append(
                    f"{cfg_path} cli={existing_cli}; refuse to overwrite "
                    f"(this cwd belongs to a non-codex driver)"
                )
                return result

        # preserve 用户字段, force driver-owned 字段. 关键: mcp.server 老 backfill
        # 可能写过 stale 值, 强 normalize 成 "pre" 防止下次误归因.
        cfg = dict(existing)
        cfg["cli"] = "codex"
        cfg["driver_type"] = self.type_name
        cfg.setdefault("mode", mode)
        cfg.setdefault("tmux_session", tmux_session)
        cfg.setdefault("project_name", project_name)
        cfg.setdefault("start_command", "codex")
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
                        f"# {project_name} — PreToolUse Rules (codex)\n\n"
                        f"Driver 内嵌 evaluator 评估 Codex approval, 叠加此规则到"
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

        # 3. pre_rule/agents/<dir>/agent_pointer.json (driver 索引)
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
                elif (existing_ptr.get("cli") or "claude") != "codex":
                    result.conflicts.append(
                        f"{pointer_path} cli={existing_ptr.get('cli')!r} but expected codex"
                    )
                else:
                    result.skipped.append(pointer_path)
        else:
            pointer = {
                "cwd": target_dir,
                "agent_id": agent_id,
                "cli": "codex",
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
                f"tmux session '{tmux_session}' not running. To spawn codex:\n"
                f"  bash {spawn_script} {agent_id}"
            )

        result.ok = (not result.failures
                     and not result.conflicts
                     and has_session(tmux_session, timeout=1.0))
        return result

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
