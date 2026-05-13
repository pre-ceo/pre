"""
pre tmux 助手 — 独立 module, 不依赖其他 pre 内部模块.

为 driver / analyzer / 任何子系统共享 tmux 操作工具.
重要: analyzer.py 里有同名 send_to_tmux, 是历史实现; 本 module 是为 driver 提供干净依赖。
"""
from __future__ import annotations
import shutil
import subprocess
import threading
import time
from typing import Optional


# inject provenance — stuck_detector 仅对此处登记的 driver 注入 auto-Enter,
# 防 Claude Code v2 ghost-text / 用户 paste 等未知来源被误提交.
# 失败 (Enter 被吞 / paste detection) 留登记给 stuck_detector retry; 成功提交清登记.
_outstanding_injects: dict[str, dict] = {}
_outstanding_lock = threading.Lock()


def _register_inject(session: str, text: str) -> None:
    with _outstanding_lock:
        _outstanding_injects[session] = {"text": text, "ts": time.time()}


def _clear_inject(session: str) -> None:
    with _outstanding_lock:
        _outstanding_injects.pop(session, None)


def get_outstanding_inject(session: str) -> Optional[dict]:
    """stuck_detector 用 — 返该 session 上一次 send_to_tmux 未确认提交的 inject.
    返 None 表示无登记 (pending_text 必属 ghost-text / 用户 paste 等其他来源, 严禁 auto-Enter).
    """
    with _outstanding_lock:
        rec = _outstanding_injects.get(session)
        return dict(rec) if rec else None


def clear_outstanding_inject(session: str) -> None:
    """stuck_detector 成功 retry Enter 后清登记."""
    _clear_inject(session)


def find_tmux() -> str:
    return shutil.which("tmux") or "tmux"


def has_session(session: str, timeout: float = 3.0) -> bool:
    # exact match (=) 防 prefix bug (016 sister, agent-ceo audit 002)
    # 旧: -t fn_homelab 在已有 fn_homelab_bpi 时 partial match 误返 True
    try:
        r = subprocess.run(
            [find_tmux(), "has-session", "-t", f"={session}"],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def send_to_tmux(session: str, text: str, timeout: float = 5.0,
                  max_retry: int = 1) -> bool:
    """
    向 tmux session 注入 text 并提交 (Enter).
    注入后验证 ❯ 输入框清空, 没清就重发 Enter, 最多 max_retry 次.
    max_retry 默认 3→1 — claude code v2.x Ink UI 已稳定,
       多次 Enter 在 ask UI 期间会误选默认 1=Yes (危险副作用).
       Ink UI 偶发 stuck 仍可显式传 max_retry=3 调用.
    多行文本 (>=10 行 \n) 会被 claude code v2 的 paste detection
       识别为 paste, 渲染成 `[Pasted text #N +M lines]` 占位符. 0.3s 不够让
       paste 检测窗口结束 (Enter 被吞进 paste 内容); 改 1.0s. 同时
       _is_input_clear_of 学会识别 paste 占位符为"未清", 触发 retry Enter.
    长单行 (e.g. 1700 字 JSON dispatch) 不渲染成 paste 占位符
       但仍触发 cli paste detection (基于字符数 burst). 修补阈值:
       len >= 300 OR \n >= 10 都走长 sleep, 拉到 1.5s.
    """
    if not session or not text:
        return False
    tmux = find_tmux()
    if not has_session(session, timeout=timeout):
        return False
    # 长 / 多行 text → 走 paste 路径, sleep 拉长到 1.5s
    # 之前仅看 \n >= 10, 漏掉长单行 (e.g. agent-ceo 1700 字 JSON dispatch).
    # cli paste detection 也基于字符数 burst, 短时间大量字符同样触发.
    # 阈值: \n >= 10 OR len >= 300 走长 sleep. 1.5s (从 1.0s 提) 给更宽容的窗口.
    line_count = text.count("\n")
    text_len = len(text)
    is_long = (line_count >= 10) or (text_len >= 300)
    pre_enter_sleep = 1.5 if is_long else 0.3
    try:
        # 1. literal text
        r1 = subprocess.run(
            [tmux, "send-keys", "-t", session, "-l", text],
            capture_output=True, text=True, timeout=timeout,
        )
        if r1.returncode != 0:
            return False
        # inject 落 pane 后立刻登记 provenance, 给 stuck_detector 区分
        # "我们注入但 Enter 失败" vs "ghost-text/paste 未知来源".
        _register_inject(session, text)
        # 2. wait for Ink UI render (paste detection 也需要更长窗口)
        time.sleep(pre_enter_sleep)
        # 3. Enter — paste mode 下 Enter 会触发 submit, retry 兜底
        # 注意: ask UI 期间不该 retry Enter (会误选 1=Yes).
        # _is_input_clear_of 会优先检测 ask UI → 返 True 跳过 retry.
        for attempt in range(max_retry + 1):  # +1: 至少跑一次 Enter, 然后再可 retry max_retry 次
            subprocess.run(
                [tmux, "send-keys", "-t", session, "Enter"],
                capture_output=True, text=True, timeout=timeout,
            )
            time.sleep(0.5 if attempt == 0 else 0.8)  # 第一次等 0.5s, 后续等更久
            if _is_input_clear_of(session, text, timeout=timeout):
                _clear_inject(session)  # 已确认提交, 清登记
                return True
            # 没清 (paste 占位符还在 ❯ 行) → 再发 Enter 兜底
        # 全部 retry 失败: 留登记给 stuck_detector retry Enter (provenance 已建立)
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _is_input_clear_of(session: str, text: str, timeout: float = 3.0) -> bool:
    """检测 ❯ 输入框是否已清掉 text (即 prompt 已被提交).
    判定: pane 末尾 5 行不含 text 前 60 字符的子串, 或末尾出现 esc to interrupt (busy 状态).
    加 ask UI 检测 — 如果末 5 行出现 `Do you want to ...?`, 算清了
       (text 已交给 cli 触发了工具调用 ask, 不需要 retry Enter, 否则 Enter 默认选 1=Yes 自动批准).
    加 paste 占位符检测 — 多行 text 被 claude code 渲染成
       `[Pasted text #N +M lines]`, text[:60] 不会出现在 pane 里, 之前误判 "清了".
       现在: 检测 ❯ 行含 `[Pasted text` → 未清, 触发 retry Enter.
    """
    pane = capture_pane(session, lines=10, timeout=timeout)
    if not pane:
        return False
    tail = "\n".join(pane.splitlines()[-5:])
    # 如果 pane 末尾有"esc to interrupt" → 已进入 busy, 算清了
    if "esc to interrupt" in tail:
        return True
    # 如果 pane 末尾有 ⏺ 工具调用行 → 已开始处理, 算清了
    if "⏺" in tail:
        return True
    # 如果末尾出现 ask UI 提示 (Do you want to make/proceed/create) → 已触发 ask, 算清了
    # 否则 retry Enter 会误选 1=Yes 自动批准 (危险)
    if "Do you want to" in tail:
        return True
    # paste 占位符 → 未清 (Enter 被 paste detection 吞了, 需 retry)
    # 模式: 任意 ❯ 行后跟 [Pasted text
    for line in tail.splitlines():
        s = line.strip()
        if s.startswith("❯") and "[Pasted text" in s:
            return False
    # 否则看输入框 (❯ 行) 是否还含 text (前 60 字)
    snippet = text[:60]
    if snippet and snippet in tail:
        return False  # 还在输入框
    return True


def is_input_pending(session: str, timeout: float = 3.0) -> tuple[bool, str]:
    """检测 agent 输入框是否有未提交的预输入文本 (stuck 状态).
    返回 (is_stuck, pending_text).
    is_stuck=True 表示 ❯ 后有非空文本但 agent 不在 busy 也不在 ask UI."""
    pane = capture_pane(session, lines=15, timeout=timeout)
    if not pane:
        return False, ""
    lines = pane.splitlines()
    tail = "\n".join(lines[-10:])
    # 排除 busy / ask UI
    if "esc to interrupt" in tail:
        return False, ""
    if "Do you want to proceed?" in tail:
        return False, ""
    # 找 ❯ 行的内容
    pending_text = ""
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("❯"):
            after = s.lstrip("❯").strip()
            if after and after not in ("?", ""):
                # 排除 idle / cli-内置提示 (加 queued messages placeholder)
                # cli busy 时 user 输入会进 queue, input box 显示 "Press up to edit queued messages",
                # 这是 cli 正常 UI, 不该被 stuck_detector 当 pending text 误干预
                if "for shortcuts" in after or "/clear" in after \
                        or "Press up to edit queued" in after:
                    return False, ""
                # paste 占位符也算 pending (一个或多个 [Pasted text #N +M lines] 卡在输入框)
                pending_text = after[:200]
            break
    return bool(pending_text), pending_text


def send_key(session: str, key: str, timeout: float = 3.0) -> bool:
    """
    发送单个原生按键 (不走 literal 模式, 不补 Enter).
    给 menu 选择 / Escape / 单字符决策用. key 可以是 "1" "2" "3" "Escape" "Up" 等 tmux key 名.
    Claude Code v2 的 ask UI 数字键通常直接 confirm, 不需要再 Enter.
    """
    if not session or not key:
        return False
    tmux = find_tmux()
    if not has_session(session, timeout=timeout):
        return False
    try:
        r = subprocess.run(
            [tmux, "send-keys", "-t", session, key],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def capture_pane(session: str, lines: int = 200, timeout: float = 5.0) -> str:
    """capture pane 当前内容 (倒数 lines 行)"""
    tmux = find_tmux()
    try:
        r = subprocess.run(
            [tmux, "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
