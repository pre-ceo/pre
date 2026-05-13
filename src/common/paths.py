"""pre 路径 single source of truth — module-level constants resolved at import.

Constants:
  PRE_ROOT       — pre 仓库根 (从 __file__ 自定位 — 允许的 __file__ 用法, 脚本知道自己在哪)
  PRE_RULE_ROOT  — $PRE_RULE_ROOT env (scripts/install.sh 写入 ~/.pre/env) → sibling 推算 fallback
  PRE_LOG_ROOT   — $PRE_LOG_DIR env (scripts/install.sh 写入) → sibling 推算 fallback

设计原则:
  - 不假设任何特定父目录
  - 仅 __file__ 自定位 + 可选 env override
  - install.sh 显式探测一次写 ~/.pre/env, 所有 caller 通过本模块取值

加载流程:
  1. import 时 inline 加载 ~/.pre/env 到 os.environ (跟 token_resolver._load_env_file 等价,
     避免 import 循环 / sys.path 顺序问题, 不依赖 token_resolver)
  2. 解析常量 (env-first, sibling fallback)

使用:
  from common.paths import PRE_RULE_ROOT, PRE_LOG_ROOT
  log_file = Path(PRE_LOG_ROOT) / "findings" / "WARNING-foo.md"
"""
import os

# === Step 1: eager load ~/.pre/env 到 os.environ (idempotent: k not in os.environ check) ===
_env_file = os.path.expanduser("~/.pre/env")
if os.path.isfile(_env_file):
    try:
        with open(_env_file, encoding="utf-8") as _f:
            for _line in _f:
                _s = _line.strip()
                if not _s or _s.startswith("#") or "=" not in _s:
                    continue
                _k, _, _v = _s.partition("=")
                _k, _v = _k.strip(), _v.strip()
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                    _v = _v[1:-1]
                if _k and _k not in os.environ:
                    os.environ[_k] = _v
    except OSError:
        pass
del _env_file

# === Step 2: 解析常量 (env-first + __file__ self-locate sibling fallback) ===
# __file__ = pre/src/common/paths.py → dirname^3 = pre/
_HERE = os.path.dirname(os.path.abspath(__file__))
PRE_ROOT = os.environ.get(
    "PRE_ROOT",
    os.path.dirname(os.path.dirname(_HERE)),  # common -> src -> pre
)

PRE_RULE_ROOT = os.environ.get(
    "PRE_RULE_ROOT",
    os.path.normpath(os.path.join(PRE_ROOT, "..", "pre_rule")),
)

PRE_LOG_ROOT = os.environ.get(
    "PRE_LOG_DIR",
    os.path.normpath(os.path.join(PRE_ROOT, "..", "pre_log")),
)

# Agent projects 工作目录 root (sibling projects to pre, e.g. fn_pre, agent-X).
# 优先 env (install.sh 可加 --agent-home), fallback sibling: pre 仓库的 parent.
# 不假设特定父目录, 仅 __file__ 自定位 + 可选 env override.
PRE_AGENT_HOME = os.environ.get(
    "PRE_AGENT_HOME",
    os.path.dirname(PRE_ROOT),  # pre 仓库 parent dir
)
