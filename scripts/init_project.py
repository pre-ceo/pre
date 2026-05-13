#!/usr/bin/env python3
"""DEPRECATED: 旧的 init_project.py 已合并到 scripts/pre_init.py.

本文件保留作向后兼容 shim — 转发给 pre_init.main(). 新代码直接用 pre_init.py.

差异:
- pre_init.py 接受 positional target_dir (默认 cwd); init_project.py 总用 cwd.
- pre_init.py 额外支持 --project-name / --model / --role / --no-templates.
- pre_init.py 写 agent_pointer.json (pre_rule/agents/<dir>/ 是 driver 索引指针).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import pre_init  # noqa: E402


if __name__ == "__main__":
    print("[init_project.py] deprecated shim — invoking scripts/pre_init.py",
          file=sys.stderr)
    sys.exit(pre_init.main())
