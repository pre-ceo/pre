"""pre_mcp — pre mcp server (4 核心工具).

agent ↔ master 主路径: agent 通过 stdio JSON-RPC 调本子进程, 由本子进程
经 loopback HTTP (urllib stdlib) 转发到 master. master/hooks 仍 stdlib only;
仅本子进程引 mcp SDK.
"""
__version__ = "0.1.0"
