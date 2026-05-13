#!/usr/bin/env python3
"""
pre: Claude Code Stop hook — 纯观测模式

只做: 记录日志 + 检测 finding + 发通知
不做: 不 block, 不给 next_action, 不干预 agent 下一步

Agent 的持续运行由项目自身的 CLAUDE.md / pre/ 规则驱动, 不由 stop hook 控制。
"""
import sys
import os
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.logger import log_event
from src.analyzer import load_agent_config
from src.governor import ensure_agent_dir
from src.reporter import report_finding

# pre_log: 优先 $PRE_LOG_DIR (install.sh 写入, 已由 `from src.config import` 间接触发
# token_resolver eager load), fallback 到 sibling 推算.
LOG_ROOT = os.environ.get(
    "PRE_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "pre_log"),
)


def main():
    cfg = load_config()

    # --- 解析 stdin ---
    try:
        input_data = json.load(sys.stdin)
    except Exception:
        return output_passthrough()

    session_id = input_data.get("session_id", "unknown")
    cwd = input_data.get("cwd", "")
    stop_reason = input_data.get("stop_reason", "")
    transcript_path = input_data.get("transcript_path", "")

    project_name = os.path.basename(cwd) if cwd else "unknown"
    agent_pre_dir = ensure_agent_dir(cfg.pre_base_dir, cwd)

    # Stop hook 自身可能收不到 transcript_path, 从 PreToolUse 保存的文件读取
    if not transcript_path:
        saved_path_file = os.path.join(agent_pre_dir, "transcript_path.txt")
        try:
            if os.path.isfile(saved_path_file):
                with open(saved_path_file) as f:
                    transcript_path = f.read().strip()
        except OSError:
            pass

    has_transcript = bool(transcript_path and os.path.isfile(transcript_path))
    print(f"[pre] stop | {project_name} | transcript={'YES' if has_transcript else 'NO'}", file=sys.stderr)
    agent_config = load_agent_config(agent_pre_dir, cwd)
    mode = agent_config.get("mode", "supervised")

    now = datetime.now(timezone.utc)
    ts_short = now.strftime("%H:%M:%S")

    # --- 记录日志 ---
    entry = {
        "ts": now.isoformat(),
        "event": "stop",
        "mode": mode,
        "cwd": cwd,
        "session": session_id[:12],
        "stop_reason": stop_reason,
    }
    log_event(cfg.log_dir, entry)

    # --- 详细 stop 日志 ---
    _write_stop_log(project_name, now, session_id, stop_reason, mode)

    print(f"[pre] {ts_short} STOP | {project_name} | mode={mode}", file=sys.stderr)

    # --- 检测 finding (从项目 pre/findings/ 目录读取待处理的 finding) ---
    _process_pending_findings(cwd, session_id)

    # --- 异步启动分析 (独立进程, 不阻塞 hook) ---
    # supervised 也启 analyzer, 但 analyzer 内部按 mode 决定行为
    # (supervised → 生成 proposals 给 GUI 选; autonomous/freerun → tmux 注入)
    if cwd and has_transcript:
        _spawn_async_analyzer(session_id, cwd, transcript_path, stop_reason)

    # --- tmux 直发 prompt 留档 (fire-and-forget) ---
    if cwd and has_transcript:
        _spawn_prompt_logger(cwd, transcript_path)

    # --- 检查完成标记 (agent 写 pre/.done 表示任务完成) ---
    done_file = os.path.join(cwd, "pre", ".done") if cwd else ""
    if done_file and os.path.isfile(done_file):
        try:
            os.remove(done_file)  # 一次性标记, 用完删除
        except OSError:
            pass
        print(f"[pre] {project_name} | .done marker found, letting agent stop", file=sys.stderr)
        return output_passthrough()

    # --- 连续 stop 检测 (rate limit / agent 无法工作) ---
    if mode in ("freerun", "autonomous"):
        consecutive = _count_consecutive_stops(cfg.log_dir, cwd)
        if consecutive >= 3:
            print(f"[pre] {project_name} | {consecutive} consecutive stops without tool calls, letting agent stop", file=sys.stderr)
            return output_passthrough()

    # --- 决策 ---
    # freerun/autonomous: passthrough 让 agent 停下来等待输入
    # analyzer 异步完成后通过 tmux send-keys 注入指令 (模拟用户输入)
    if mode in ("freerun", "autonomous"):
        print(f"[pre] {project_name} | passthrough, analyzer will inject via tmux", file=sys.stderr)
    return output_passthrough()


def _count_consecutive_stops(log_dir: str, cwd: str) -> int:
    """从日志末尾往前数连续 stop event, 遇到工具调用就停止计数"""
    if not os.path.isdir(log_dir):
        return 0

    files = sorted(
        [f for f in os.listdir(log_dir) if f.startswith("pre_hook_") and f.endswith(".jsonl")],
    )

    events = []
    for fname in files[-2:]:
        fpath = os.path.join(log_dir, fname)
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        if e.get("cwd") == cwd:
                            events.append(e)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue

    count = 0
    for e in reversed(events):
        if e.get("event") == "stop":
            count += 1
        else:
            break
    return count


def _process_pending_findings(cwd: str, session_id: str):
    """
    检查项目 pre/findings/ 目录下是否有待处理的 finding 文件
    格式: {level}-{title}.md, 内容是 finding 描述
    处理后移到 pre/findings/processed/
    """
    findings_dir = os.path.join(cwd, "pre", "findings")
    if not os.path.isdir(findings_dir):
        return

    processed_dir = os.path.join(findings_dir, "processed")
    project_name = os.path.basename(cwd)

    for fname in os.listdir(findings_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(findings_dir, fname)
        if not os.path.isfile(fpath):
            continue

        # 解析文件名: INFO-title.md / WARNING-title.md / CRITICAL-title.md
        parts = fname.replace(".md", "").split("-", 1)
        if len(parts) != 2:
            continue
        level = parts[0].upper()
        if level not in ("INFO", "WARNING", "CRITICAL"):
            continue
        title = parts[1]

        try:
            with open(fpath) as f:
                content = f.read()
        except OSError:
            continue

        # 报告 + 通知 + git tag
        result = report_finding(
            cwd=cwd,
            level=level,
            title=title,
            content=content,
            session_id=session_id,
        )
        print(f"[pre] {project_name} | FINDING [{level}] {title} | report={result.get('report_path','')} notified={result.get('notified',False)}", file=sys.stderr)

        # 移到 processed/
        os.makedirs(processed_dir, exist_ok=True)
        try:
            os.rename(fpath, os.path.join(processed_dir, fname))
        except OSError:
            pass


def _consume_next_action(cwd: str) -> str:  # [被 260427 tmux 注入方案替代] 保留备用
    """
    读取并删除 {cwd}/pre/.next_action 文件 (一次性消费)
    返回文件内容, 不存在或异常返回空字符串
    """
    if not cwd:
        return ""
    next_action_file = os.path.join(cwd, "pre", ".next_action")
    if not os.path.isfile(next_action_file):
        return ""
    try:
        with open(next_action_file) as f:
            content = f.read().strip()
        os.remove(next_action_file)
        return content
    except OSError:
        return ""


def output_continue(cwd: str, mode: str = "freerun", override_reason: str = ""):  # [被 260427 tmux 注入方案替代] 保留备用
    """block 停止, 让 agent 读项目规则自行决定下一步"""

    # 如果 analyzer 提供了具体指令, 直接用
    if override_reason:
        done_instruction = (
            "When you are certain the task is fully complete, run: "
            "echo done > pre/.done — this will signal the system to let you stop."
        )
        if mode == "autonomous":
            reason = f"{override_reason}\n\nIf fully done, {done_instruction}"
        else:
            reason = override_reason
        result = {
            "decision": "block",
            "reason": reason,
        }
        print(json.dumps(result))
        sys.exit(0)

    next_file = os.path.join(cwd, "pre", "next.md") if cwd else ""

    done_instruction = (
        "When you are certain the task is fully complete, run: "
        "echo done > pre/.done — this will signal the system to let you stop."
    )

    if next_file and os.path.isfile(next_file):
        if mode == "autonomous":
            reason = (
                f"Read {next_file} and follow the instructions. "
                f"If you have completed the user's original request and all follow-up items, "
                f"{done_instruction}"
            )
        else:
            reason = f"Read {next_file} and follow the instructions to decide your next task."
    else:
        if mode == "autonomous":
            reason = (
                f"Check if you have completed the user's original request. "
                f"If not done, read CLAUDE.md and continue working. "
                f"If fully done, {done_instruction}"
            )
        else:
            reason = (
                "Read your project's CLAUDE.md and pre/ directory to decide what to do next. "
                "If all tasks are complete, check for pending TODOs, run tests, or review code quality."
            )

    result = {
        "decision": "block",
        "reason": reason,
    }
    print(json.dumps(result))
    sys.exit(0)


def output_passthrough():
    """supervised: 不干预"""
    print(json.dumps({}))
    sys.exit(0)


def _spawn_prompt_logger(cwd: str, transcript_path: str):
    """独立进程跑 log_user_prompt.py, 抓 transcript 最近 user prompt 上报 master 留档"""
    import subprocess
    project_name = os.path.basename(cwd)
    if not project_name:
        return
    agent_id = f"local.cli-claude-code-local.{project_name}"
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_user_prompt.py")
    try:
        subprocess.Popen(
            [sys.executable, script, agent_id, transcript_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _spawn_async_analyzer(session_id: str, cwd: str,
                          transcript_path: str, stop_reason: str):
    """
    fork + detach 后台分析进程, 立即返回, 不阻塞 hook
    分析结果由 stop_analyzer.py 写到 pre_log/
    """
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop_analyzer.py")
    try:
        subprocess.Popen(
            [sys.executable, script, session_id, cwd, transcript_path or "", stop_reason or ""],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach, 父进程结束不影响
        )
    except OSError:
        pass


def _write_stop_log(project_name: str, ts: datetime, session_id: str,
                    stop_reason: str, mode: str):
    """写 stop 日志到 pre_log/{project}/stop_YYYYMMDD.jsonl"""
    log_dir = os.path.join(LOG_ROOT, project_name)
    os.makedirs(log_dir, exist_ok=True)

    date_str = ts.strftime("%Y%m%d")
    log_file = os.path.join(log_dir, f"stop_{date_str}.jsonl")

    entry = {
        "ts": ts.isoformat(),
        "session": session_id[:12],
        "mode": mode,
        "stop_reason": stop_reason,
    }

    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
