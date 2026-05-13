"""
pre 配置模块
Dataclass 配置模式 (参考 weaselbn/src/config.py)
零外部依赖, 仅 stdlib

配置层级:
  1. pre_rule/ (用户级, 与 pre 同级目录) — 全局规则、运行时数据、日志
  2. pre/ (代码仓库) — 可移植的代码, 不含用户数据
  3. {project}/pre/ (项目级) — 项目特定规则和配置
"""
from dataclasses import dataclass
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 加载 ~/.pre/env (single source by scripts/install.sh) — eager via token_resolver import.
# 顺序: ensure pre/ in sys.path → import src.common.token_resolver → 触发 _load_env_file.
import sys
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from src.common import token_resolver  # noqa: F401 — side effect: load ~/.pre/env

# pre_rule: 优先 $PRE_RULE_ROOT (install.sh 写入), fallback 到 sibling 推算.
RULE_ROOT = os.environ.get(
    "PRE_RULE_ROOT",
    os.path.join(os.path.dirname(PROJECT_ROOT), "pre_rule"),
)


@dataclass
class HookConfig:
    """PreToolUse hook 运行时配置"""
    mode: str = "observe"       # observe: 仅记录+ask | enforce: 调用 governor 决策
    log_dir: str = ""           # 日志目录
    verbose: bool = True        # True: 记录完整 tool_input | False: 仅记录关键字段
    pre_base_dir: str = ""      # agent 运行时数据目录
    rules_dir: str = ""         # 全局规则目录
    governor_timeout: int = 0   # 超时秒数 (0=不限时)
    governor_provider: str = "claude"  # claude | gemini


def load_config() -> HookConfig:
    """
    加载配置: pre_rule/config.json → 环境变量 → 默认值
    所有路径默认指向 pre_rule/ 目录
    """
    # 优先从 pre_rule/config.json 加载
    config_path = os.environ.get(
        "PRE_CONFIG",
        os.path.join(RULE_ROOT, "config.json")
    )

    cfg = HookConfig()

    if os.path.exists(config_path):
        with open(config_path) as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

    # 默认路径: 全部指向 pre_rule/
    if not cfg.log_dir:
        cfg.log_dir = os.path.join(RULE_ROOT, "logs")
    elif not os.path.isabs(cfg.log_dir):
        cfg.log_dir = os.path.join(RULE_ROOT, cfg.log_dir)

    if not cfg.pre_base_dir:
        cfg.pre_base_dir = os.path.join(RULE_ROOT, "agents")
    elif not os.path.isabs(cfg.pre_base_dir):
        cfg.pre_base_dir = os.path.join(RULE_ROOT, cfg.pre_base_dir)

    if not cfg.rules_dir:
        cfg.rules_dir = RULE_ROOT  # global.md 直接在 pre_rule/ 根目录
    elif not os.path.isabs(cfg.rules_dir):
        cfg.rules_dir = os.path.join(RULE_ROOT, cfg.rules_dir)

    return cfg
