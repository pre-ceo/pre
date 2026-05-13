"""
pre Driver 抽象基类 — 所有 driver 必须实现此接口
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentSpec:
    """driver 发现 agent 时的描述"""
    agent_id: str               # 全局唯一: <node_id>.<driver_type>.<local_name>
    role: str = "worker"
    capabilities: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class BaseDriver:
    """
    所有 driver 继承本类.
    生命周期: init → discover_agents → (send / get_state) ... → shutdown
    """

    type_name: str = "base"

    async def init(self, node_ctx) -> None:
        """driver 启动. node_ctx 包含 node_id, config 等"""
        self.node_ctx = node_ctx

    async def discover_agents(self) -> list[AgentSpec]:
        """枚举本 driver 当前可见的 agent"""
        return []

    async def send(self, agent_id: str, message: dict) -> bool:
        """给 agent 发消息. 返回是否成功"""
        return False

    async def get_state(self, agent_id: str) -> str:
        """查 agent 当前状态: idle/busy/blocked/error/offline"""
        return "unknown"

    async def detect_pending(self, agent_id: str) -> Optional[dict]:
        """
        二次检测 agent 是否在等待人类决策 (UI 卡在 ask).
        返回 None 表示不在等待; 返回 dict 含 agent_id/tool_kind/description/since_pane_ts.
        """
        return None

    async def decide(self, agent_id: str, key: str) -> bool:
        """
        远程注入按键给 agent 的 UI (1/2/3/Escape 等), 替代人类操作.
        返回是否成功注入.
        """
        return False

    async def detect_activity(self, agent_id: str) -> Optional[dict]:
        """
        当前活动状态 + 最近动作 (capture-pane 派生).
        返回 None 表示无信息; 否则 dict 含:
          state: idle|busy|blocked_user|thinking
          last_action: 最近一次工具调用摘要 (例 "Bash: uv run python ...")
          pane_summary: 最后 5 行 pane 文本 (供 GUI 进一步显示)
          since_ts: 检测时间
        """
        return None

    async def shutdown(self) -> None:
        """清理资源"""
        pass
