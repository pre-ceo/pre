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


@dataclass
class InitResult:
    """init_agent 返回值. 所有 driver 共用.

    ok=True: 所有步骤就位且 tmux session 在跑.
    ok=False: conflicts/failures/next_steps 给修复指引.

    幂等保证: 重跑同 target_dir 不破坏用户已有内容; cli-specific 设置 (例如
    claude 的 .claude/settings.json hook) 冲突 → 进 conflicts (不强改);
    pointer 已存在且 cwd/cli 不一致 → 进 conflicts.
    """
    ok: bool
    agent_id: str
    target_dir: str
    created: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    next_steps: list = field(default_factory=list)
    failures: list = field(default_factory=list)


class BaseDriver:
    """
    所有 driver 继承本类.
    生命周期: init → discover_agents → (send / get_state) ... → shutdown
    """

    type_name: str = "base"
    cli_name: str = "base"  # 跟 agent_config.json 的 cli 字段对应 (claude/codex/gemini)

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

    async def init_agent(self, target_dir: str, opts: Optional[dict] = None) -> InitResult:
        """幂等初始化一个 agent 到 target_dir.

        共用契约 (各 driver 子类实现具体步骤):
          1. validate target_dir (绝对路径, 存在)
          2. 写 target_dir/pre/agent_config.json (cli=cli_name)
          3. cli-specific 接入 (claude 写 .claude/settings.json hook;
             codex/gemini 跳过 — 走 driver 内嵌 evaluator)
          4. 写 pre_rule/agents/<dir>/agent_pointer.json
          5. tmux session check (用户起 spawn_agent.sh)

        opts 可选: mode, tmux_session, project_name, model, role,
          write_claude_settings (claude only), write_templates.
        """
        return InitResult(
            ok=False,
            agent_id="",
            target_dir=target_dir,
            failures=[f"init_agent not implemented for driver {self.type_name}"],
        )

    async def shutdown(self) -> None:
        """清理资源"""
        pass
