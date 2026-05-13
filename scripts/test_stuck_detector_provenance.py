#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_stuck_detector_provenance.py

验证 inject provenance 状态机:
  - send_to_tmux 成功提交 → 登记被清
  - send_to_tmux 失败 (留 pending) → 登记仍在
  - get_outstanding_inject 返回正确 record / None
  - 模拟 stuck_detector 决策路径: pending_text 跟 inject 严格全等才 Enter

不开真 tmux session — 所有 IO 路径测的是 module-level state machine + 比较
逻辑, 真 tmux 行为 (Enter 是否被吞 / paste detection 行为) 留给手动 e2e.
"""
import os
import sys
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, "..", "src"))

from tmux_helper import (  # noqa: E402
    _register_inject,
    _clear_inject,
    get_outstanding_inject,
    clear_outstanding_inject,
)


def case_register_and_get():
    _register_inject("session_a", "hello inbox msg")
    rec = get_outstanding_inject("session_a")
    assert rec is not None
    assert rec["text"] == "hello inbox msg"
    assert isinstance(rec["ts"], float)
    print("[OK] case_register_and_get")


def case_get_miss_returns_none():
    rec = get_outstanding_inject("nonexistent_session_xyz")
    assert rec is None
    print("[OK] case_get_miss_returns_none")


def case_clear_via_public_api():
    _register_inject("session_b", "to be cleared")
    assert get_outstanding_inject("session_b") is not None
    clear_outstanding_inject("session_b")
    assert get_outstanding_inject("session_b") is None
    print("[OK] case_clear_via_public_api")


def case_overwrite_register():
    _register_inject("session_c", "first")
    rec1 = get_outstanding_inject("session_c")
    time.sleep(0.01)
    _register_inject("session_c", "second")
    rec2 = get_outstanding_inject("session_c")
    assert rec2["text"] == "second"
    assert rec2["ts"] >= rec1["ts"]
    _clear_inject("session_c")
    print("[OK] case_overwrite_register")


def case_stuck_decision_logic_match():
    """模拟 stuck_detector: pending_text 跟 inject 严格全等 → 应 Enter."""
    inject_text = "短文本完全一致"
    _register_inject("session_d", inject_text)
    pending_text = inject_text  # is_input_pending 不截断 (短文本)
    inject = get_outstanding_inject("session_d")
    truncated = inject["text"][:200]
    assert pending_text == truncated, "match expected"
    clear_outstanding_inject("session_d")
    print("[OK] case_stuck_decision_logic_match")


def case_stuck_decision_logic_mismatch_ghost():
    """ghost-text 场景: 没 register → get_outstanding_inject None → 跳过."""
    pending_text = "Claude Code ghost-text 自动预填的内容"
    inject = get_outstanding_inject("session_e_no_register")
    assert inject is None, "ghost-text 应无 inject 登记 → 严禁 auto-Enter"
    print("[OK] case_stuck_decision_logic_mismatch_ghost")


def case_stuck_decision_logic_mismatch_text():
    """有 register 但 pending_text 跟 inject 不同 → 跳过 (e.g. 用户在我们 inject 之后又 paste 了别的)."""
    _register_inject("session_f", "我们注入的 inbox 消息")
    pending_text = "用户后来 paste 进来覆盖了"
    inject = get_outstanding_inject("session_f")
    truncated = inject["text"][:200]
    assert pending_text != truncated, "应不匹配 → 跳过 auto-Enter"
    _clear_inject("session_f")
    print("[OK] case_stuck_decision_logic_mismatch_text")


def case_long_text_200_truncation():
    """is_input_pending 把 pending_text 截 200 字, inject 长文本必须 inject_text[:200] 比对."""
    inject_text = "a" * 250
    _register_inject("session_g", inject_text)
    pending_text = "a" * 200  # is_input_pending 截断后
    inject = get_outstanding_inject("session_g")
    truncated = inject["text"][:200]
    assert pending_text == truncated, "200-char 截断比对应匹配"
    _clear_inject("session_g")
    print("[OK] case_long_text_200_truncation")


def case_paste_placeholder_does_not_match():
    """paste 占位符 [Pasted text #N +M lines] 跟原文不同 → 跳过 (保守 A 方案)."""
    inject_text = "x" * 500  # 长 inject 触发 paste
    _register_inject("session_h", inject_text)
    pending_text = "[Pasted text #2 +15 lines]"  # claude code v2 渲染的占位符
    inject = get_outstanding_inject("session_h")
    truncated = inject["text"][:200]
    assert pending_text != truncated, "paste 占位符不应匹配 → 跳过 auto-Enter"
    _clear_inject("session_h")
    print("[OK] case_paste_placeholder_does_not_match")


def main():
    print("=== stuck_detector inject provenance test ===")
    case_register_and_get()
    case_get_miss_returns_none()
    case_clear_via_public_api()
    case_overwrite_register()
    case_stuck_decision_logic_match()
    case_stuck_decision_logic_mismatch_ghost()
    case_stuck_decision_logic_mismatch_text()
    case_long_text_200_truncation()
    case_paste_placeholder_does_not_match()
    print("=== ALL OK ===")


if __name__ == "__main__":
    main()
