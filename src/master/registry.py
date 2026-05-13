"""
Master 内存 registry — Node + Agent 实时状态

WriteThrough 到 sqlite (persistence)。
"""
from __future__ import annotations
import time
from typing import Optional


class NodeInfo:
    def __init__(self, node_id: str, host: str, capabilities: list,
                 ws_writer=None):
        self.node_id = node_id
        self.host = host
        self.capabilities = capabilities
        self.last_seen = time.time()
        self.online = True
        self.ws_writer = ws_writer  # 用于 master → node 推送

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "host": self.host,
            "capabilities": self.capabilities,
            "last_seen": self.last_seen,
            "online": self.online,
        }


class AgentInfo:
    def __init__(self, agent_id: str, node_id: str, driver_type: str,
                 role: str, state: str = "idle",
                 capabilities: list = None, metadata: dict = None):
        self.agent_id = agent_id
        self.node_id = node_id
        self.driver_type = driver_type
        self.role = role
        self.state = state
        self.capabilities = capabilities or []
        self.metadata = metadata or {}
        self.last_update = time.time()

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "node_id": self.node_id,
            "driver_type": self.driver_type,
            "role": self.role,
            "state": self.state,
            "capabilities": self.capabilities,
            "metadata": self.metadata,
            "last_update": self.last_update,
        }


class Registry:
    """
    内存 + sqlite 持久化双写.
    所有方法在 asyncio loop 单线程调用, 不加锁.
    """

    def __init__(self, db):
        self.db = db
        self.nodes: dict[str, NodeInfo] = {}
        self.agents: dict[str, AgentInfo] = {}
        # pending 检测: agent_id -> dict(tool_kind, description, since_pane_ts, ...)
        # 不持久化, 每次心跳全量替换 (按 node)
        self.pending: dict[str, dict] = {}
        # agent activity (state/last_action/pane_summary), 同 pending 模式 10s 心跳
        self.activity: dict[str, dict] = {}
        # agent task summary (LLM 生成 20 字短语), 60s 后台轮询更新
        self.task_summaries: dict[str, dict] = {}  # {agent_id: {summary, ts}}
        # agent stop 后 supervised analyzer 生成的 next-step proposals (GUI 让用户选)
        self.proposals: dict[str, dict] = {}  # {agent_id: {proposals: [...], ts}}
        # 用户 dismiss 后 mute 标记, mute 中 stop_analyzer 不重新生成 proposals (防循环)
        self.proposals_muted: set[str] = set()
        # LLM cli 配额监控 (sys_claude/sys_gemini/sys_codex 周期 probe)
        # LLM cli 配额监控 (sys_claude/sys_gemini/sys_codex 周期 probe)
        # 加 node 维度.
        # registry.usage 顶层 = local 数据 (向后兼容 pre_ui usage 现有 GUI)
        # registry.usage_by_node[node_id] = {claude, gemini, codex, severity, probed_ts}
        self.usage: dict = {}
        self.usage_by_node: dict[str, dict] = {}
        # Phase A:
        # collector heartbeat last_seen, lazy stale 检测 (>120s 触 finding HIGH).
        # [remote-node+local-only hack 自 G10]
        self.collector_last_seen: dict[str, float] = {}

        # 启动时不从 sqlite 恢复 online 状态 — 重启 master 视为所有 node offline,
        # 等 node 重连时主动注册
        for n in db.list_nodes():
            db.mark_node_offline(n["node_id"])

        # 启动时从 sqlite load 历史 agent, state 一律覆盖为 "stale" —
        # master 刚重启不知道 agent 实际状态, 等 node register 来覆盖回 idle/failed.
        # 没被覆盖的就是真的 "未接管" (driver 不再 yield 的老项目). GUI 按 last_update
        # 倒序排, stale 的自然沉底. 用户语义: 历史出现过但死了的项目也显示, 标未接管.
        for a_dict in db.list_agents():
            md = dict(a_dict.get("metadata") or {})
            md["status"] = "stale"
            md.setdefault("failure_reason", "master-restart-pending-rediscover")
            md.setdefault("failure_hint",
                          "agent was registered before master restart; "
                          "awaiting node re-register (or driver no longer yields it).")
            info = AgentInfo(
                agent_id=a_dict["agent_id"],
                node_id=a_dict.get("node_id") or "",
                driver_type=a_dict.get("driver_type") or "",
                role=a_dict.get("role") or "worker",
                state="stale",
                capabilities=a_dict.get("capabilities") or [],
                metadata=md,
            )
            info.last_update = a_dict.get("last_update") or info.last_update
            self.agents[info.agent_id] = info

    # ---------- Pending ----------
    def replace_pending_for_node(self, node_id: str, pending_list: list[dict]):
        """该 node 全量替换 pending. 同 agent 同 description 保留首次发现时间."""
        # 当前 node 名下旧 pending 留个 snapshot 用于 dedup
        old_for_node = {aid: p for aid, p in self.pending.items()
                        if p.get("node_id") == node_id}
        # 清掉本 node 的所有 agent 的旧 pending
        for aid in old_for_node:
            self.pending.pop(aid, None)
        for p in pending_list:
            aid = p.get("agent_id")
            if not aid:
                continue
            p["node_id"] = node_id
            old = old_for_node.get(aid)
            if old and old.get("description") == p.get("description"):
                # 同一个 ask 还在等, 保留 since_pane_ts (首次发现时间)
                p["since_pane_ts"] = old.get("since_pane_ts", p.get("since_pane_ts"))
            self.pending[aid] = p

    def list_pending(self) -> list[dict]:
        return list(self.pending.values())

    def get_pending(self, agent_id: str):
        return self.pending.get(agent_id)

    # ---------- Activity () ----------
    def replace_activity_for_node(self, node_id: str, activity_list: list[dict]):
        """该 node 全量替换 agent 活动状态. 不持久化, 只内存."""
        if not hasattr(self, "activity"):
            self.activity = {}
        old_for_node = {aid: v for aid, v in self.activity.items()
                        if v.get("node_id") == node_id}
        # 清掉本 node 的旧 activity
        for aid in old_for_node:
            self.activity.pop(aid, None)
        for a in activity_list:
            aid = a.get("agent_id")
            if aid:
                a["node_id"] = node_id
                old = old_for_node.get(aid) or {}
                old_fp = old.get("_activity_fingerprint")
                new_fp = self._activity_fingerprint(a)
                if old_fp == new_fp:
                    a["last_activity_ts"] = old.get("last_activity_ts")
                else:
                    a["last_activity_ts"] = a.get("since_ts")
                a["_activity_fingerprint"] = new_fp
                self.activity[aid] = a

    @staticmethod
    def _activity_fingerprint(a: dict) -> tuple:
        # 内存态 best-effort UI 排序锚, 非审计戳; 排除 since_ts (heartbeat 会刷)
        recent = a.get("recent_actions") or []
        recent_fp = tuple(
            (r.get("tool"), r.get("summary")) for r in recent
            if isinstance(r, dict)
        )
        return (
            a.get("state"),
            a.get("last_action"),
            a.get("tool_kind"),
            recent_fp,
            a.get("last_response_excerpt"),
            a.get("claude_status"),
            a.get("pane_summary"),
        )

    def get_activity(self, agent_id: str):
        if not hasattr(self, "activity"):
            self.activity = {}
        return self.activity.get(agent_id)

    # ---------- Task summary (LLM 生成) ----------
    def set_task_summary(self, agent_id: str, summary: str | None):
        import time
        if not hasattr(self, "task_summaries"):
            self.task_summaries = {}
        if summary is None:
            self.task_summaries.pop(agent_id, None)
        else:
            self.task_summaries[agent_id] = {"summary": summary, "ts": time.time()}

    def get_task_summary(self, agent_id: str) -> dict | None:
        if not hasattr(self, "task_summaries"):
            self.task_summaries = {}
        return self.task_summaries.get(agent_id)

    # ---------- Proposals () ----------
    def set_proposals(self, agent_id: str, proposals: list[dict]):
        import time
        if not hasattr(self, "proposals"):
            self.proposals = {}
        if proposals:
            self.proposals[agent_id] = {"proposals": proposals, "ts": time.time()}
        else:
            self.proposals.pop(agent_id, None)

    def get_proposals(self, agent_id: str) -> dict | None:
        if not hasattr(self, "proposals"):
            self.proposals = {}
        return self.proposals.get(agent_id)

    def clear_proposals(self, agent_id: str):
        if hasattr(self, "proposals"):
            self.proposals.pop(agent_id, None)

    # mute 控制
    def mute_proposals(self, agent_id: str):
        if not hasattr(self, "proposals_muted"):
            self.proposals_muted = set()
        self.proposals_muted.add(agent_id)

    def unmute_proposals(self, agent_id: str):
        if hasattr(self, "proposals_muted"):
            self.proposals_muted.discard(agent_id)

    def is_proposals_muted(self, agent_id: str) -> bool:
        if not hasattr(self, "proposals_muted"):
            self.proposals_muted = set()
        return agent_id in self.proposals_muted

    # ---------- Node ----------
    def add_node(self, info: NodeInfo):
        self.nodes[info.node_id] = info
        self.db.upsert_node(info.node_id, info.host, info.capabilities,
                            info.last_seen, online=True)

    def remove_node(self, node_id: str):
        info = self.nodes.pop(node_id, None)
        if info:
            info.online = False
            self.db.mark_node_offline(node_id)
            # 该 node 下所有 agent 标记 offline
            for aid, a in list(self.agents.items()):
                if a.node_id == node_id:
                    a.state = "offline"
                    a.last_update = time.time()
                    self.db.upsert_agent(aid, a.node_id, a.driver_type, a.role,
                                         a.state, a.capabilities, a.metadata,
                                         a.last_update)

    def touch_node(self, node_id: str):
        info = self.nodes.get(node_id)
        if info:
            info.last_seen = time.time()
            self.db.upsert_node(info.node_id, info.host, info.capabilities,
                                info.last_seen, online=True)

    def get_node(self, node_id: str) -> Optional[NodeInfo]:
        return self.nodes.get(node_id)

    def list_nodes(self) -> list[dict]:
        return [n.to_dict() for n in self.nodes.values()]

    # ---------- Agent ----------
    def upsert_agent(self, info: AgentInfo):
        self.agents[info.agent_id] = info
        self.db.upsert_agent(info.agent_id, info.node_id, info.driver_type,
                             info.role, info.state, info.capabilities,
                             info.metadata, info.last_update)

    def remove_agent(self, agent_id: str):
        a = self.agents.pop(agent_id, None)
        if a:
            a.state = "offline"
            a.last_update = time.time()
            self.db.upsert_agent(a.agent_id, a.node_id, a.driver_type, a.role,
                                 a.state, a.capabilities, a.metadata,
                                 a.last_update)

    def update_agent_state(self, agent_id: str, state: str):
        a = self.agents.get(agent_id)
        if a:
            a.state = state
            a.last_update = time.time()
            self.db.upsert_agent(a.agent_id, a.node_id, a.driver_type, a.role,
                                 a.state, a.capabilities, a.metadata,
                                 a.last_update)

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        return self.agents.get(agent_id)

    def list_agents(self) -> list[dict]:
        """按 last_update 倒序排. stale/dead 的自然沉底, 最近活跃的在顶."""
        items = [a.to_dict() for a in self.agents.values()]
        items.sort(key=lambda d: d.get("last_update") or 0, reverse=True)
        return items
