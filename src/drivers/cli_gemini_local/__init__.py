"""cli-gemini-local driver — 接 Gemini CLI agent 到 pre bus.

agent 发现: 扫 pre_rule/agents/<dir>/agent_pointer.json, pointer.cli == "gemini".
agent_id: <node_id>.cli-gemini-local.<project_name>

Gemini CLI 原生有 `gemini hooks` 子命令 (类似 claude code), 配置在
~/.gemini/settings.json. 长期 plan: 走 hook 路径; 第一版用 codex 模式
(driver 内嵌 evaluator + pane scrape + auto allow/deny).
"""
from __future__ import annotations

from .driver import CliGeminiLocalDriver

DRIVER = CliGeminiLocalDriver()
