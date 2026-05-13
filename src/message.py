"""
pre Message Bus — Message 数据结构

跨 Master / Node / Driver / Agent 流转的基本单位。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import time
import uuid


# kind 枚举 (用 str 不用 Enum, JSON 序列化方便)
KIND_COMMAND = "command"
KIND_REPORT = "report"
KIND_CHAT = "chat"
KIND_EVENT = "event"
KIND_HEARTBEAT = "heartbeat"
KIND_ACK = "ack"
KIND_RESULT = "result"

VALID_KINDS = {
    KIND_COMMAND, KIND_REPORT, KIND_CHAT,
    KIND_EVENT, KIND_HEARTBEAT, KIND_ACK, KIND_RESULT,
}


@dataclass
class Message:
    """
    总线消息.
    agent_id 格式: <node_id>.<driver_type>.<local_name>
    """
    from_agent: str
    kind: str
    payload: dict
    to_agent: Optional[str] = None
    from_role: str = ""
    to_role: Optional[str] = None
    parent_id: Optional[str] = None
    priority: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        # 容错: 缺字段时用默认值
        return cls(
            from_agent=d.get("from_agent", ""),
            kind=d.get("kind", KIND_EVENT),
            payload=d.get("payload", {}),
            to_agent=d.get("to_agent"),
            from_role=d.get("from_role", ""),
            to_role=d.get("to_role"),
            parent_id=d.get("parent_id"),
            priority=d.get("priority", 0),
            id=d.get("id") or uuid.uuid4().hex,
            ts=d.get("ts") or time.time(),
        )
