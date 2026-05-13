"""
cli-claude-code-local driver
对接本机的 Claude Code agent (通过 pre 现有 hook + tmux 通信).

agent 发现: 扫 pre_rule/agents/<dir>/agent_pointer.json (driver 唯一索引),
            pointer.cwd 指向 agent 实际目录, 该目录下 pre/agent_config.json 是配置真源.
agent_id: <node_id>.cli-claude-code-local.<project_name>
send: send_to_tmux(tmux_session, text)
get_state: 读 stop_status.json

配置不全 / pointer 缺失 / hook 未装 / tmux session 不在 → status=failed.
failed agent 仍在 discover 列表 (master/GUI 看得见), 但 send/decide 等交互 short-circuit.
迁移老 pre_rule/agents/<dir>/ (无 pointer) 用 scripts/pre_migrate.py.
"""
from __future__ import annotations
import json
import os
import sys

# 复用现有 pre 模块
_PRE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(_PRE_ROOT, "src"))

import hashlib
import re
import subprocess
import time
from typing import Optional

from drivers.base import BaseDriver, AgentSpec, InitResult
from tmux_helper import send_to_tmux, send_key, capture_pane, has_session


# claude code v2.1 ask UI 标志字串 — 检测时**同时**至少匹配 "Do you want" 系列 (精确)
# 单独 "❯ 1." / "Esc to cancel" 太通用 (agent 在对话/代码里讨论 pending 检测引用就会误判)
# fix: 之前 agent-fe 自己讨论 PENDING_MARKERS 字符串列表被误判 blocked_user
# fix: claude 实际渲染会插 "to <filename>" 在中间 (例 "Do you want to make this edit to foo.md?"),
# 故 PRIMARY 去掉结尾 "?" 改 prefix 匹配, 兼容含/不含文件名两种形态
_PENDING_PRIMARY = (
    "Do you want to proceed?",
    "Do you want to make this edit",
    "Do you want to make this change",
    "Do you want to create",
)
# 副标志: 出现需配合主标志才算
_PENDING_SECONDARY = ("❯ 1.", "Esc to cancel", "1. Yes")
# 兼容旧名 (其他模块可能引用)
PENDING_MARKERS = _PENDING_PRIMARY + _PENDING_SECONDARY


# 网络环境 cache (cwd → network_info, 30min TTL).
# 不轮询 (HC-G10 + ~/.claude/CLAUDE.md ): discover 时第一次跑, 后续命中 cache.
_NETWORK_CACHE: dict[str, tuple[float, dict]] = {}
_NETWORK_TTL_SEC = 1800  # 30min


_NETWORK_INFLIGHT: set[str] = set()  # 防同 cwd 多次 inflight
_NETWORK_LOCK = None  # lazy init threading.Lock
# A3: cache miss 后 _bg 填好 cache 后, 加 cwd 进 pending,
# register_loop 检查到 → 触发 re-register (让 metadata.network 字段生效)
_NETWORK_RE_REGISTER_PENDING: set[str] = set()


def take_pending_reregister() -> list[str]:
    """register_loop 调: 取并清空 pending re-register cwd 列表 (避免重复触发).
    返 cwd list, 调用方按 cwd 找对应 agent re-register."""
    global _NETWORK_RE_REGISTER_PENDING
    if _NETWORK_LOCK is None:
        return []
    with _NETWORK_LOCK:
        out = list(_NETWORK_RE_REGISTER_PENDING)
        _NETWORK_RE_REGISTER_PENDING.clear()
    return out


def _probe_network_cached(cwd: str) -> Optional[dict]:
    """探测 agent cwd 出口 IP + 代理. cache hit 返立即;
    miss 启后台 thread fire-and-forget 探 (不阻塞 discover), 本次返 None.
    下一轮 discover (10s 后) 命中 cache."""
    if not cwd:
        return None
    if not os.path.isdir(cwd):
        return None
    now = time.time()
    cached = _NETWORK_CACHE.get(cwd)
    if cached and (now - cached[0]) < _NETWORK_TTL_SEC:
        print(f"[network-cache] HIT {os.path.basename(cwd)}: {cached[1].get('exit_ip','?')}", flush=True)
        return cached[1]
    print(f"[network-cache] MISS {os.path.basename(cwd)}, cache_keys={[os.path.basename(k) for k in _NETWORK_CACHE.keys()]}", flush=True)
    # miss → 后台探, 本次返 None (不阻塞 discover_agents)
    global _NETWORK_LOCK
    if _NETWORK_LOCK is None:
        import threading as _th
        _NETWORK_LOCK = _th.Lock()
    with _NETWORK_LOCK:
        if cwd in _NETWORK_INFLIGHT:
            return None
        _NETWORK_INFLIGHT.add(cwd)

    def _bg():
        try:
            info = _do_probe_network(cwd)
            if info:
                _NETWORK_CACHE[cwd] = (time.time(), info)
                # A3: 加进 pending re-register, register_loop 触发重 register
                with _NETWORK_LOCK:
                    _NETWORK_RE_REGISTER_PENDING.add(cwd)
                print(f"[network-probe] {os.path.basename(cwd)}: {info.get('exit_ip')} "
                      f"({info.get('country')})", flush=True)
            else:
                print(f"[network-probe] {os.path.basename(cwd)}: failed (no info)", flush=True)
        except Exception as e:
            print(f"[network-probe] {os.path.basename(cwd)}: exception {e}", flush=True)
        finally:
            with _NETWORK_LOCK:
                _NETWORK_INFLIGHT.discard(cwd)

    import threading as _th
    _th.Thread(target=_bg, daemon=True, name=f"net-probe-{os.path.basename(cwd)}").start()
    return None


def _do_probe_network(cwd: str) -> Optional[dict]:
    """实际 curl ip.lzq.dev/json. 走 cwd 自己的 env (HTTP_PROXY 等). fail-safe."""
    try:
        # 用 curl 而不是 urllib, 因为 curl 自动读 cwd/.env 和系统 env 的代理设置
        r = subprocess.run(
            ["curl", "-s", "-m", "8", "https://ip.lzq.dev/json"],
            cwd=cwd, capture_output=True, text=True, timeout=12,
        )
        if r.returncode != 0:
            return {"error": f"curl exit {r.returncode}", "probed_ts": time.time()}
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"error": "bad json", "probed_ts": time.time()}
        # 只保留关键字段, 不存敏感
        return {
            "exit_ip": data.get("ip"),
            "country": data.get("country"),
            "city": data.get("city"),
            "isp": data.get("isp") or data.get("org"),
            "via_proxy": bool(data.get("proxy") or data.get("hosting")),
            "probed_ts": time.time(),
        }
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"error": str(e)[:200], "probed_ts": time.time()}


def _extract_llm_route(cwd: str) -> Optional[dict]:
    """读 cwd/.claude/settings.json + settings.local.json 的 env 块,
    检测目录级 LLM 路由覆盖. 返 dict 或 None.

    背景: claude code 支持目录级 .claude/settings.json 的 env 块覆盖 OAuth 默认,
    关键 env: ANTHROPIC_BASE_URL (自定义网关 endpoint), ANTHROPIC_AUTH_TOKEN (Bearer),
    ANTHROPIC_API_KEY (X-Api-Key), ANTHROPIC_MODEL.

    返回字段 (任一非默认才返 dict, 全默认返 None):
      - base_url: str | None (具体值, e.g. https://my-gateway.com)
      - model: str | None (e.g. claude-opus-4-7)
      - has_auth_token: bool (有 ANTHROPIC_AUTH_TOKEN)
      - has_api_key: bool (有 ANTHROPIC_API_KEY)
      - source: list[str] (哪些文件贡献了 env, e.g. ["settings.json", "settings.local.json"])

    敏感值 (token/key 实际内容) 不放 metadata, 只标 has_*=True/False.
    """
    if not cwd or not os.path.isdir(cwd):
        return None
    merged_env = {}
    sources = []
    # 顺序: settings.json 然后 settings.local.json (后者覆盖前者, 跟 cli 行为一致)
    for fname in ("settings.json", "settings.local.json"):
        path = os.path.join(cwd, ".claude", fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        env = doc.get("env") or {}
        if isinstance(env, dict):
            for k, v in env.items():
                if k.startswith("ANTHROPIC_") and isinstance(v, (str, int, float)):
                    merged_env[k] = v
            if env:
                sources.append(fname)
    if not merged_env:
        return None
    out = {
        "base_url": merged_env.get("ANTHROPIC_BASE_URL") or None,
        "model": merged_env.get("ANTHROPIC_MODEL") or None,
        "has_auth_token": "ANTHROPIC_AUTH_TOKEN" in merged_env,
        "has_api_key": "ANTHROPIC_API_KEY" in merged_env,
        "source": sources,
    }
    # 全字段 falsy → 实际没覆盖, 返 None
    if not (out["base_url"] or out["model"] or out["has_auth_token"] or out["has_api_key"]):
        return None
    return out


def _is_pane_pending(pane: str) -> bool:
    """精确判断 pane 是否在 ask UI.
    claude 真实 ask UI 必在 pane **最末尾** (最后被渲染的 box), 表现为:
      - "Do you want to proceed?" / "Do you want to make this edit?" 等 primary 字串
      - 紧跟 "❯ 1. Yes" 选项 (或 "Esc to cancel · Tab to amend" 提示行)
      - 之后没有任何 ⏺ 工具调用行 (工具调用一定发生在 ask UI 之前)
    严格条件: pane 最末尾 12 行内 primary + secondary 同时出现, 且这之后无 ⏺ 行.
    claude code v2 渲染会在 ask UI 后留大量空白行垫底, 取末尾固定 12 行可能全空错过 ask;
    先跳过尾部空白行, 再取最后 12 个非空行作 tail."""
    lines = pane.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    tail = lines[-12:]
    tail_text = "\n".join(tail)
    has_primary = any(p in tail_text for p in _PENDING_PRIMARY)
    has_secondary = any(s in tail_text for s in _PENDING_SECONDARY)
    if not (has_primary and has_secondary):
        return False
    # 找到 primary 的位置, 之后不应有 ⏺ 工具调用行
    for i, line in enumerate(tail):
        if any(p in line for p in _PENDING_PRIMARY):
            after = tail[i + 1:]
            for line2 in after:
                if "⏺" in line2 and "Bash" in line2 or "⏺" in line2 and "Update" in line2:
                    return False  # primary 之后有工具调用 = 它已经被批准过了, 不是当前 ask
            break
    return True
# 抽 last action header 的关键词 (按 capture-pane 倒序找最近一个)
ACTION_HEADERS_RE = re.compile(
    r"(Bash command|Bash\(|Edit\(|Edit file|Read\(|Write\(|Glob\(|Grep\(|"
    r"WebFetch\(|WebSearch\(|TodoWrite\(|Task\(|Skill\()"
)


# PreToolUse / Stop hook 的可识别 command needle. 老项目用 python3 路径写 .py
# 后缀; install.sh PR 后的新项目用 console_script shim (pre-tool-use / pre-stop-hook,
# 装在 ~/.local/bin/). 任一命中即认为是 pre 装的 hook.
_PRE_HOOK_NEEDLES = ("pre_tool_use.py", "pre-tool-use")
_PRE_STOP_NEEDLES = ("stop_hook.py", "pre-stop-hook")


def _cmd_matches_any(cmd: str, needles) -> bool:
    return bool(cmd) and any(n in cmd for n in needles)


def _has_pre_hook_installed(cwd: str) -> bool:
    """检查 cwd/.claude/settings*.json 是否含 pre 的 PreToolUse hook.
    兼容 pre_tool_use.py (旧) + pre-tool-use shim (新). settings.json 与
    settings.local.json 任一命中即认为装了."""
    for fname in ("settings.json", "settings.local.json"):
        path = os.path.join(cwd, ".claude", fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        hooks = doc.get("hooks", {}) or {}
        for group in hooks.get("PreToolUse", []) or []:
            for h in group.get("hooks", []) or []:
                if _cmd_matches_any(h.get("command", "") or "", _PRE_HOOK_NEEDLES):
                    return True
    return False


class CliClaudeCodeLocalDriver(BaseDriver):
    type_name = "cli-claude-code-local"
    cli_name = "claude"

    async def init(self, node_ctx):
        await super().init(node_ctx)
        # pre_rule/agents 路径 (driver 索引根; 跟 src/config.py:RULE_ROOT 同步, 相对 pre sibling)
        self.rule_root = os.environ.get(
            "PRE_RULE_ROOT",
            os.path.normpath(os.path.join(_PRE_ROOT, "..", "pre_rule")),
        )
        self.agents_dir = os.path.join(self.rule_root, "agents")
        self.node_id = node_ctx.get("node_id", "local")

    async def discover_agents(self) -> list[AgentSpec]:
        """扫 pre_rule/agents/<dir>/agent_pointer.json. pointer.cwd 指向 agent 实际目录,
        该目录的 pre/agent_config.json 是配置 single source of truth.

        每个 agent 都会被 yield, 即使配置不全 — 配置缺一项就 status=failed,
        failure_reason 标识哪里坏了 (master/GUI 看得见, send/decide 等会拒绝交互).

        迁移老 pre_rule/agents/<dir>/ (无 pointer) 跑 scripts/pre_migrate.py.
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
        """单 pre_rule/agents/<name>/ → AgentSpec, 或 None (非 claude 归别的 driver).

        检查链: pointer → cwd → cwd/pre/agent_config.json → hook → tmux session.
        任一缺失返 spec(status=failed, failure_reason, failure_hint).
        """
        pointer_path = os.path.join(agent_pre_dir, "agent_pointer.json")

        # 1. pointer 必须存在 (老格式 → orphan, 仍 yield 让 GUI 提示用户跑 pre_migrate.py)
        if not os.path.isfile(pointer_path):
            return self._failed_spec(
                f"orphan-{name}",
                reason="no-pointer",
                hint=f"missing {pointer_path}; run scripts/pre_migrate.py",
                extra={"pre_rule_dir": agent_pre_dir},
            )

        # 2. pointer 必须可解析
        try:
            with open(pointer_path) as f:
                pointer = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return self._failed_spec(
                f"invalid-{name}",
                reason="invalid-pointer",
                hint=f"corrupt {pointer_path}: {e}",
                extra={"pre_rule_dir": agent_pre_dir},
            )

        # 3. cli 分流: 非 claude 留给对应 driver, 本 driver 返 None 跳过
        if (pointer.get("cli") or "claude") != "claude":
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

        # 4. cwd 必须真实存在
        if not os.path.isdir(cwd):
            return self._failed_spec(
                project_name,
                reason="cwd-missing",
                hint=f"cwd not found on disk: {cwd}",
                extra=common_extra,
            )

        # 5. cwd/pre/agent_config.json 是配置真源 (用户决策: 规则/配置以 agent 目录的 pre 为准)
        cfg_path = os.path.join(cwd, "pre", "agent_config.json")
        if not os.path.isfile(cfg_path):
            return self._failed_spec(
                project_name,
                reason="not-initialized",
                hint=f"missing {cfg_path}; run pre-init in {cwd}",
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
        if cfg_cli != "claude":
            return self._failed_spec(
                project_name,
                reason="cli-mismatch",
                hint=f"pointer cli=claude but {cfg_path} cli={cfg_cli}",
                extra=common_extra,
            )

        tmux_session = cfg.get("tmux_session") or project_name
        mode = cfg.get("mode", "supervised")
        runtime_extra = dict(common_extra)
        runtime_extra["tmux_session"] = tmux_session
        runtime_extra["mode"] = mode

        # 6. hook 必须装好 (.claude/settings*.json 含 pre_tool_use.py)
        if not _has_pre_hook_installed(cwd):
            return self._failed_spec(
                project_name,
                reason="hook-not-installed",
                hint=f"{cwd}/.claude/settings.json missing pre hook; run pre-init",
                extra=runtime_extra,
            )

        # 7. tmux session 必须活着 (运行时态)
        if not has_session(tmux_session, timeout=2.0):
            return self._failed_spec(
                project_name,
                reason="tmux-session-missing",
                hint=(f"tmux session '{tmux_session}' not running; "
                      f"spawn via scripts/spawn_agent.sh {agent_id}"),
                extra=runtime_extra,
            )

        # 全部通过 → status=ok
        if cfg.get("role"):
            role = cfg["role"]
        elif mode in ("freerun", "autonomous"):
            role = "freerun-worker"
        else:
            role = "worker"
        llm_route = _extract_llm_route(cwd)
        cli_model = cfg.get("model") or (
            llm_route.get("model") if llm_route else None
        )
        network = _probe_network_cached(cwd)

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
                "llm_route": llm_route,
                "cli": "claude",
                "cli_model": cli_model,
                "network": network,
            },
        )

    def _failed_spec(self, slug: str, *, reason: str, hint: str,
                     extra: Optional[dict] = None) -> AgentSpec:
        """构造 status=failed 的 AgentSpec. slug 决定 agent_id 末段."""
        meta: dict = {
            "status": "failed",
            "failure_reason": reason,
            "failure_hint": hint,
            "cli": "claude",
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
        """发消息: 用 tmux send-keys 注入到 agent 的 tmux pane.
        failed agent 拒绝交互 (status != ok 时 short-circuit)."""
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
                text = payload.get("text") or payload.get("prompt") or json.dumps(payload, ensure_ascii=False)
                return send_to_tmux(ts, text)
        return False

    async def get_state(self, agent_id: str) -> str:
        """读 stop_status.json. failed agent 返 "failed"."""
        for spec in await self.discover_agents():
            if spec.agent_id == agent_id:
                if not self._is_active(spec):
                    return "failed"
                # stop_status 在 pre_rule_dir (driver 索引目录) 下
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
        """capture-pane 扫一段, 判断是否在 ask UI 状态. failed agent 返 None."""
        for spec in await self.discover_agents():
            if spec.agent_id != agent_id:
                continue
            if not self._is_active(spec):
                return None
            ts = spec.metadata.get("tmux_session", "")
            if not ts:
                return None
            pane = capture_pane(ts, lines=80)
            if not pane or not _is_pane_pending(pane):
                return None
            tool_kind, description = _extract_pending_action(pane)
            return {
                "agent_id": agent_id,
                "tool_kind": tool_kind,
                "description": description[:200],
                "since_pane_ts": time.time(),
                "tmux_session": ts,
            }
        return None

    async def decide(self, agent_id: str, key: str) -> bool:
        """注入按键到 agent 的 ask UI. failed agent 拒绝."""
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
        """从 tmux pane 抽 state + recent_actions + last_response + claude_status.
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
            # state 判断 (严格化, 必须 primary+secondary 同段)
            has_pending = _is_pane_pending(pane)
            # busy 检测: 仅看 pane 末尾 10 行 (claude 当前进行时 status 一定在末尾)
            # 之前扫整个 pane 200 行会命中历史段, 例如 "esc to interrupt" 在历史 Bash 调用区
            # idle 时末尾会显示 "? for shortcuts ... new task? /clear to save"
            # claude v2 渲染会末尾留白, 先 trim 再取 tail
            _lines = pane.splitlines()
            while _lines and not _lines[-1].strip():
                _lines.pop()
            tail_for_busy = "\n".join(_lines[-10:])
            busy_markers = ("Simmering…", "Pouncing…", "Pondering…", "Cooking…",
                            "Crunching…", "Sautéing…", "Working…", "esc to interrupt")
            idle_markers = ("? for shortcuts", "new task?", "/clear to save")
            has_busy = any(b in tail_for_busy for b in busy_markers)
            has_idle_marker = any(m in tail_for_busy for m in idle_markers)
            # idle marker 优先 (claude 显式说"等输入" → 一定是 idle)
            if has_idle_marker:
                has_busy = False
            if has_pending:
                state = "blocked_user"
            elif has_busy:
                state = "busy"
            else:
                state = "idle"

            tool_kind, last_action = _extract_pending_action(pane)
            recent_actions = _extract_recent_actions(pane, n=5)
            last_response_excerpt = _extract_last_response(pane)
            claude_status = _extract_claude_status(pane)

            # pane_summary 扩到 2000 字, 末尾若干非空行
            tail_lines = [l for l in pane.splitlines() if l.strip()][-30:]
            pane_summary = "\n".join(tail_lines)[:2000]
            # pane fingerprint, master decide 重试用 (字节级判 ask 区是否被消化)
            pane_fp = hashlib.sha1(pane_summary.encode("utf-8")).hexdigest()

            return {
                "agent_id": agent_id,
                "state": state,
                "last_action": last_action[:200],
                "tool_kind": tool_kind,
                "recent_actions": recent_actions,
                "last_response_excerpt": last_response_excerpt,
                "claude_status": claude_status,
                "pane_summary": pane_summary,
                "pane_fp": pane_fp,
                "tmux_session": ts,
                "since_ts": time.time(),
            }
        return None

    async def init_agent(self, target_dir: str, opts: Optional[dict] = None) -> InitResult:
        """幂等初始化一个 claude agent.

        5 步: validate → cwd/pre/ + agent_config → .claude/settings hook → pre_rule pointer → tmux check.

        重跑同 target_dir 不破坏: 已存在文件跳过, hook 冲突报 conflicts (不强改),
        pointer cwd 不一致报 conflicts. ok=True 表示一切就位且 tmux session 在跑.

        opts (全部可选):
          mode, tmux_session, project_name, model, role,
          write_claude_settings (bool, 默认 True), write_templates (bool, 默认 True)
        """
        opts = opts or {}
        result = InitResult(ok=False, agent_id="", target_dir=target_dir)

        # 1. validate target_dir
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
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path) as f:
                    existing = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                result.failures.append(f"existing {cfg_path} unreadable: {e}")
                return result
            existing_cli = existing.get("cli") or "claude"
            if existing_cli != "claude":
                result.conflicts.append(
                    f"{cfg_path} cli={existing_cli}; refuse to overwrite "
                    f"(this cwd belongs to a non-claude driver)"
                )
                return result
            result.skipped.append(cfg_path)
        else:
            cfg = {
                "cli": "claude",
                # driver_type 跟 type_name 一致, pre_mcp _caller_from_agent_config
                # fallback 用它拼 caller_agent_id (3 段式 <node>.<driver>.<project>).
                "driver_type": self.type_name,
                "mode": mode,
                "tmux_session": tmux_session,
                "project_name": project_name,
            }
            if opts.get("model"):
                cfg["model"] = opts["model"]
            if opts.get("role"):
                cfg["role"] = opts["role"]
            try:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                result.created.append(cfg_path)
            except OSError as e:
                result.failures.append(f"write {cfg_path}: {e}")
                return result

        if opts.get("write_templates", True):
            rules_path = os.path.join(pre_dir, "rules.md")
            if not os.path.isfile(rules_path):
                with open(rules_path, "w", encoding="utf-8") as f:
                    f.write(
                        f"# {project_name} — PreToolUse Rules\n\n"
                        f"Governor 评估工具调用时叠加此规则到全局规则之上.\n\n"
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
                        f"autonomous/freerun 模式时 stop hook 引导 agent 读此文件决定下一步.\n"
                    )
                result.created.append(next_path)
            else:
                result.skipped.append(next_path)

        # 3. .claude/settings.json hook (冲突时不强改)
        if opts.get("write_claude_settings", True):
            settings_dir = os.path.join(target_dir, ".claude")
            settings_path = os.path.join(settings_dir, "settings.json")
            # hook command 用 ~/.local/bin shim 短名 (走 PATH, 不写本机路径).
            # shim 由 scripts/install.sh 一次性装 (与 ~/.pre/env 同 single source);
            # 这里只 check 已装否, fail-fast 提示 user 跑 install.sh.
            _shim = os.path.expanduser("~/.local/bin/pre-tool-use")
            if not os.path.isfile(_shim):
                result.failures.append(
                    f"shim {_shim} not installed; run "
                    f"`bash {_PRE_ROOT}/scripts/install.sh` first"
                )
                return result
            pre_hook_script = "pre-tool-use"
            stop_hook_script = "pre-stop-hook"

            settings_doc: Optional[dict] = None
            settings_was_present = os.path.isfile(settings_path)
            if settings_was_present:
                try:
                    with open(settings_path) as f:
                        settings_doc = json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    result.conflicts.append(
                        f"{settings_path} unreadable ({e}); fix or remove before re-running"
                    )
                    settings_doc = None

            if not (settings_was_present and settings_doc is None):
                if settings_doc is None:
                    settings_doc = {}
                hooks_block = settings_doc.setdefault("hooks", {})

                pre_event_changed = False

                if self._settings_has_pre_hook(hooks_block, "PreToolUse", _PRE_HOOK_NEEDLES):
                    result.skipped.append(f"PreToolUse hook (already in {settings_path})")
                elif self._settings_has_foreign_hook(hooks_block, "PreToolUse", _PRE_HOOK_NEEDLES):
                    result.conflicts.append(
                        f"{settings_path} has non-pre PreToolUse hook; "
                        f"merge manually: python3 {pre_hook_script}"
                    )
                else:
                    hooks_block.setdefault("PreToolUse", []).append({
                        "hooks": [{"type": "command",
                                   "command": pre_hook_script}],
                    })
                    result.created.append(f"PreToolUse hook in {settings_path}")
                    pre_event_changed = True

                if self._settings_has_pre_hook(hooks_block, "Stop", _PRE_STOP_NEEDLES):
                    result.skipped.append(f"Stop hook (already in {settings_path})")
                elif self._settings_has_foreign_hook(hooks_block, "Stop", _PRE_STOP_NEEDLES):
                    result.conflicts.append(
                        f"{settings_path} has non-pre Stop hook; "
                        f"merge manually: python3 {stop_hook_script}"
                    )
                else:
                    hooks_block.setdefault("Stop", []).append({
                        "hooks": [{"type": "command",
                                   "command": stop_hook_script}],
                    })
                    result.created.append(f"Stop hook in {settings_path}")
                    pre_event_changed = True

                if pre_event_changed:
                    try:
                        os.makedirs(settings_dir, exist_ok=True)
                        with open(settings_path, "w", encoding="utf-8") as f:
                            json.dump(settings_doc, f, indent=2, ensure_ascii=False)
                            f.write("\n")
                    except OSError as e:
                        result.failures.append(f"write {settings_path}: {e}")
                        return result

        # 4. pre_rule/agents/<dir>/agent_pointer.json (driver 索引)
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
                elif (existing_ptr.get("cli") or "claude") != "claude":
                    result.conflicts.append(
                        f"{pointer_path} cli={existing_ptr.get('cli')!r} but expected claude"
                    )
                else:
                    result.skipped.append(pointer_path)
        else:
            pointer = {
                "cwd": target_dir,
                "agent_id": agent_id,
                "cli": "claude",
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

        # 5. tmux session 检查
        spawn_script = os.path.join(_PRE_ROOT, "scripts", "spawn_agent.sh")
        if has_session(tmux_session, timeout=2.0):
            result.skipped.append(f"tmux session '{tmux_session}' already running")
            if any("hook in" in s for s in result.created):
                result.next_steps.append(
                    f"NEW HOOK INSTALLED. Exit claude in tmux session "
                    f"'{tmux_session}' and restart it to load the new hook."
                )
        else:
            result.next_steps.append(
                f"tmux session '{tmux_session}' not running. To spawn:\n"
                f"  1. exit any running claude in this terminal\n"
                f"  2. bash {spawn_script} {agent_id}"
            )

        result.ok = (not result.failures
                     and not result.conflicts
                     and has_session(tmux_session, timeout=1.0))
        return result

    @staticmethod
    def _settings_has_pre_hook(hooks_block: dict, event: str, needles) -> bool:
        """needles: str 或 tuple/list. 任一 needle 出现在 command 子串即命中."""
        if isinstance(needles, str):
            needles = (needles,)
        for group in hooks_block.get(event, []) or []:
            for h in group.get("hooks", []) or []:
                if _cmd_matches_any(h.get("command") or "", needles):
                    return True
        return False

    @staticmethod
    def _settings_has_foreign_hook(hooks_block: dict, event: str, pre_needles) -> bool:
        """event 下有"非 pre"的 hook (command 不含任何 pre needle)."""
        if isinstance(pre_needles, str):
            pre_needles = (pre_needles,)
        for group in hooks_block.get(event, []) or []:
            for h in group.get("hooks", []) or []:
                cmd = h.get("command") or ""
                if cmd and not _cmd_matches_any(cmd, pre_needles):
                    return True
        return False

    async def shutdown(self):
        pass


def _extract_pending_action(pane: str) -> tuple[str, str]:
    """从 pane 文本里抽 last 工具调用类型 + 一句描述. 返回 (tool_kind, description)."""
    lines = pane.splitlines()
    # 倒序找最近一个 action header
    for i in range(len(lines) - 1, -1, -1):
        m = ACTION_HEADERS_RE.search(lines[i])
        if not m:
            continue
        head = m.group(1)
        tool_kind = head.split("(")[0].strip().lower().replace(" ", "_")
        # 取该行 + 接下来 3 行 (常含命令/参数), 拼起来作 description
        snippet = " ".join(l.strip() for l in lines[i:i + 4] if l.strip())
        return tool_kind, snippet
    # 兜底: 找 "Do you want" 那行往上
    return "unknown", lines[-1].strip()[:200] if lines else "(empty pane)"


# 三个 extractor

# ⏺ 是 U+23FA, 用作 claude code 工具调用标记; ⎿ 是 U+23BF 用作结果缩进
_ACTION_LINE_RE = re.compile(r"^\s*⏺\s+([A-Z][A-Za-z]*)\s*\((.*)\)\s*$")
_ACTION_NOPAREN_RE = re.compile(r"^\s*⏺\s+([A-Z][A-Za-z]*)\s+(.*)$")
# ✻ U+273B / ✶ U+2736 是 status line 起始
_CLAUDE_STATUS_RE = re.compile(
    r"[✻✶✦✺]\s+(Cooked|Crunched|Pondering|Pouncing|Simmering|Cooking|Crunching|Working)"
    r"(?:…|\.\.\.)?\s*(?:for\s+)?([0-9hms\s]+)?"
)


def _extract_recent_actions(pane: str, n: int = 5) -> list[dict]:
    """倒序抽最近 n 个 ⏺ 工具调用行, 返回 [{tool, summary}, ...]"""
    lines = pane.splitlines()
    actions = []
    for i in range(len(lines) - 1, -1, -1):
        if len(actions) >= n:
            break
        line = lines[i]
        m = _ACTION_LINE_RE.match(line)
        if m:
            tool = m.group(1)
            args = m.group(2).strip()[:80]
            actions.append({"tool": tool, "summary": args})
            continue
        m = _ACTION_NOPAREN_RE.match(line)
        if m:
            tool = m.group(1)
            rest = m.group(2).strip()[:80]
            actions.append({"tool": tool, "summary": rest})
    return actions  # 已是倒序 (最新在前)


def _extract_last_response(pane: str) -> str:
    """抽 agent 末尾的纯文字总结 (非 ⏺/⎿/❯/✻/box drawing 行).
    返回最后一段连续的 agent 输出, 截 500 字."""
    lines = pane.splitlines()
    # 倒序收集纯文字行直到遇到 ⏺/⎿/❯/✻/box drawing 中断
    collected = []
    for line in reversed(lines):
        s = line.rstrip()
        if not s.strip():
            if collected:
                continue
            else:
                continue
        # 跳过工具调用 / 结果 / 输入框 / status / box drawing
        first_chars = s.lstrip()[:3]
        if (first_chars.startswith("⏺") or first_chars.startswith("⎿")
                or first_chars.startswith("❯") or first_chars.startswith("✻")
                or first_chars.startswith("✶") or first_chars.startswith("✦")
                or first_chars.startswith("✺")
                or "──" in s or "━━" in s):
            if collected:
                break
            else:
                continue
        # 跳过明显的提示行
        if "shortcuts" in s.lower() or "esc to" in s.lower():
            continue
        collected.append(s.strip())
    if not collected:
        return ""
    text = "\n".join(reversed(collected))
    if len(text) > 500:
        text = text[:500] + "..."
    return text


def _extract_claude_status(pane: str) -> Optional[str]:
    """抽 claude code 内部 status line, 例 '✻ Cooked for 3m 4s'"""
    lines = pane.splitlines()
    for line in reversed(lines):
        m = _CLAUDE_STATUS_RE.search(line)
        if m:
            verb = m.group(1)
            duration = (m.group(2) or "").strip() or None
            if duration:
                return f"{verb} for {duration}"
            return verb
    return None
