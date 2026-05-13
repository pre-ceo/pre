"""pre_mcp __main__ — mcp server stdio entry.

usage: uv run --directory $PRE_DIR python -m pre_mcp
       (or `uv run --directory . python -m pre_mcp` from the pre repo root.)
(由 ~/.claude.json mcpServers.pre 自动启动子进程, stdio JSON-RPC).

注册 4 工具: send_message / fetch_inbox / read_pane / cycle_state.
caller_agent_id prefix 校验; read_pane 跨 node 严禁; 限频 60/min.

启动时自动 source ~/.pre/env (KEY=VALUE 行格式) 加载 PRE_SECRET 等敏感配置.
这样 MCP 注册命令行 (claude mcp add) 不需要带 token, 轮换只改一个文件 chmod 600.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Optional


def _load_env_file(path: Path) -> None:
    """加载 KEY=VALUE 行格式的 env 文件到 os.environ.

    - 已存在的环境变量优先 (不覆盖, 让 shell 显式 export 仍然能 override)
    - # 开头的行忽略
    - 双引号 / 单引号 包裹的值会脱壳
    - 失败 silent (文件不存在 / 格式错), MCP 仍能起 — 没 PRE_SECRET 时 master 端 401, 用户能从错误推断
    """
    try:
        if not path.is_file():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                continue
            k, _, v = s.partition("=")
            k = k.strip()
            v = v.strip()
            # 脱壳引号
            if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


# 加载顺序: 系统级 ~/.pre/env → 用户已 export 的环境变量 (后者优先)
_load_env_file(Path.home() / ".pre" / "env")


try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    sys.stderr.write(
        f"[pre_mcp] mcp SDK 不可用 ({e}). 请装: uv add mcp 或 pip install mcp\n"
    )
    sys.exit(2)

from .tools import (
    tool_send_message, tool_fetch_inbox, tool_read_pane, tool_cycle_state,
)


mcp = FastMCP("pre")


@mcp.tool()
def send_message(to_agent: str, kind: str, payload: dict,
                  parent_id: Optional[str] = None) -> dict:
    """Send a message via pre bus to target agent.

    Args:
      to_agent: target agent_id (e.g. local.cli-claude-code-local.agent-ceo)
      kind: message kind (chat/command/report/...)
      payload: message payload dict
      parent_id: optional parent msg_id for threading

    Returns: {ok: bool, result: dict, latency_ms: int}
    """
    return tool_send_message(to_agent, kind, payload, parent_id=parent_id)


@mcp.tool()
def fetch_inbox(agent_id: Optional[str] = None, since: float = 0,
                 limit: int = 50, kind: Optional[str] = None) -> dict:
    """Fetch messages from pre bus addressed to agent_id.

    Args:
      agent_id: target agent_id (default: caller agent)
      since: unix ts to fetch since (default 0 = all)
      limit: max messages to return (default 50)
      kind: filter by kind (optional)

    Returns: {ok: bool, result: {messages: [...]}, latency_ms: int}
    """
    return tool_fetch_inbox(agent_id, since=since, limit=limit, kind=kind)


@mcp.tool()
def read_pane(agent_id: str, lines: int = 100,
                grep: Optional[str] = None) -> dict:
    """Read tmux pane for agent (sanitized: ANSI strip + sensitive redact).

    : target_agent_id 必同 caller node, 跨 node 严禁.

    Args:
      agent_id: target agent_id (must be same node as caller)
      lines: tail N lines (default 100)
      grep: optional grep pattern

    Returns: {ok: bool, result: {content, status, lines, ...}, latency_ms: int}
    """
    return tool_read_pane(agent_id, lines=lines, grep=grep)


@mcp.tool()
def cycle_state(agent_id: str) -> dict:
    """Get freerun cycle state for agent.

    Args:
      agent_id: target agent_id

    Returns: {ok: bool, result: {cycle_state, last_finding, ...}, latency_ms: int}
    """
    return tool_cycle_state(agent_id)


def main():
    """Entrypoint, mcp SDK 自管 stdio JSON-RPC."""
    mcp.run()


if __name__ == "__main__":
    main()
