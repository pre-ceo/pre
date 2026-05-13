#!/usr/bin/env python3
"""
pre: Claude Code PreToolUse hook 入口脚本
Phase 1 — Observe mode: 记录所有工具调用, 零干预
"""
import sys
import os

# 将项目根加入 sys.path, 使 src/ 可作为包导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.hook import main

if __name__ == "__main__":
    main()
