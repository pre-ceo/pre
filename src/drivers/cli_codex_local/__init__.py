"""cli-codex-local driver — 接 Codex agent 到 pre bus.

agent 发现: 扫 pre_rule/agents/{Users-user-cursor-xxx}/, 读
{cwd}/pre/agent_config.json, 只收 cli == "codex".
agent_id: <node_id>.cli-codex-local.<project_name>
"""
from __future__ import annotations

from .driver import CliCodexLocalDriver

DRIVER = CliCodexLocalDriver()
