"""
transcript_parser — 从 claude code transcript_path (jsonl) 抽取 mini_task 数据.

用途:
  - stop_analyzer 触发时, 解析 transcript 末 cycle, 拼成 mini_task POST body
  - 全部字面抽取, 0 LLM 调用
  - cycle 切分: 按 type=user 真 prompt 划分 (含 type=text block, 不含 tool_result)

API:
  parse_last_cycle(transcript_path) -> dict | None
  parse_cycles(transcript_path) -> list[dict]
  build_mini_task_payload(cycle, agent_id, parent_dispatch_id) -> dict

 引入 (dev-workflow features/-mini-task-tracking-create.md).
"""
from __future__ import annotations
import json
import os
from typing import Optional


def _is_real_user_prompt(content) -> tuple[bool, str]:
    """判断 user entry 是真 prompt 还是 tool_result.
    - 真 prompt: content 是 str, 或 list 含 type=text block 不含 tool_result
    - tool_result: content 是 list 含 type=tool_result block
    返 (is_prompt, prompt_text).
    """
    if isinstance(content, str):
        return True, content
    if isinstance(content, list):
        text_parts: list[str] = []
        has_text = False
        has_tool_result = False
        for blk in content:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "text":
                has_text = True
                text_parts.append(blk.get("text") or "")
            elif t == "tool_result":
                has_tool_result = True
        if has_text and not has_tool_result:
            return True, "\n".join(text_parts)
    return False, ""


def _ts_to_epoch_ms(ts: Optional[str]) -> int:
    """ISO 时间转 ms epoch. 解析失败返 0."""
    if not ts:
        return 0
    try:
        from datetime import datetime
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def parse_cycles(transcript_path: str) -> list[dict]:
    """读 jsonl, 按真 user prompt 切 cycle.
    每个 cycle dict: {prompt_uuid, started_ts, ended_ts, request, actions, reply}.
    actions 是 list of {kind, ...} - kind in (assistant_text, tool_use, tool_result).
    """
    cycles: list[dict] = []
    current: Optional[dict] = None
    if not os.path.isfile(transcript_path):
        return cycles
    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ttype = d.get("type")
            ts = d.get("timestamp")
            if ttype == "user":
                msg = d.get("message") or {}
                c = msg.get("content")
                is_prompt, prompt_text = _is_real_user_prompt(c)
                if is_prompt:
                    if current:
                        cycles.append(current)
                    current = {
                        "prompt_uuid": (d.get("uuid") or "")[:8],
                        "started_ts": ts,
                        "ended_ts": ts,
                        "request": prompt_text,
                        "actions": [],
                        "reply": "",
                    }
                else:
                    if current and isinstance(c, list):
                        for blk in c:
                            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                                tr = blk.get("content") or ""
                                if isinstance(tr, list):
                                    tr = "\n".join(
                                        (b.get("text") or "")
                                        for b in tr
                                        if isinstance(b, dict) and b.get("type") == "text"
                                    )
                                summary = (str(tr) or "").strip()
                                current["actions"].append({
                                    "kind": "tool_result",
                                    "summary": summary[:300],
                                })
                                if ts:
                                    current["ended_ts"] = ts
            elif ttype == "assistant":
                msg = d.get("message") or {}
                c = msg.get("content")
                if not current or not isinstance(c, list):
                    continue
                for blk in c:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text":
                        text = (blk.get("text") or "").strip()
                        if not text:
                            continue
                        current["reply"] = text  # 后续会覆盖, 末次 = 最终 reply
                        current["actions"].append({
                            "kind": "assistant_text",
                            "text": text[:300],
                        })
                    elif blk.get("type") == "tool_use":
                        name = blk.get("name") or "?"
                        inp = blk.get("input") or {}
                        if isinstance(inp, dict):
                            preview_keys = ("command", "file_path", "pattern",
                                             "path", "prompt", "description")
                            preview = ""
                            for k in preview_keys:
                                if k in inp:
                                    preview = f"{k}={str(inp[k])[:120]}"
                                    break
                            if not preview:
                                preview = str(inp)[:120]
                        else:
                            preview = str(inp)[:120]
                        current["actions"].append({
                            "kind": "tool_use",
                            "name": name,
                            "input_summary": preview,
                        })
                if ts:
                    current["ended_ts"] = ts
    if current:
        cycles.append(current)
    return cycles


def parse_last_cycle(transcript_path: str) -> Optional[dict]:
    """只取最后一个 cycle (stop hook 触发时只关心当前 cycle).
    内部仍是全文解析 (jsonl 没 random access), 返末元素.
    """
    cycles = parse_cycles(transcript_path)
    return cycles[-1] if cycles else None


def build_mini_task_payload(cycle: dict, agent_id: str,
                             parent_dispatch_id: Optional[str] = None) -> dict:
    """把 cycle dict 转成 mini_task POST body schema.

    schema 字段:
      mini_task_id : agent_id + prompt 起点 ms timestamp (全局唯一)
      agent_id : 调用方传 (master registry 已知)
      request : 真 prompt 全文 (不截)
      actions : tool_use / tool_result / assistant_text 序列 (preview 截 300)
      reply : 末条 assistant text (final reply)
      started_ts : float epoch s
      ended_ts : float epoch s
      duration_sec : 派生
      tool_count : actions 中 tool_use 数 (真实工具调用次数)
      parent_dispatch_id : 调用方从 registry.activity[agent_id] 拿
      _source : 永远 "transcript_parser"
    """
    started_ms = _ts_to_epoch_ms(cycle.get("started_ts"))
    ended_ms = _ts_to_epoch_ms(cycle.get("ended_ts"))
    started_s = started_ms / 1000.0 if started_ms else 0.0
    ended_s = ended_ms / 1000.0 if ended_ms else 0.0
    duration = (ended_s - started_s) if (started_s and ended_s) else 0.0
    tool_count = sum(1 for a in cycle.get("actions") or []
                     if a.get("kind") == "tool_use")
    mini_id = f"{agent_id}-{started_ms}"
    return {
        "mini_task_id": mini_id,
        "agent_id": agent_id,
        "request": cycle.get("request") or "",
        "actions": cycle.get("actions") or [],
        "reply": cycle.get("reply") or "",
        "started_ts": started_s,
        "ended_ts": ended_s,
        "duration_sec": round(duration, 2),
        "tool_count": tool_count,
        "parent_dispatch_id": parent_dispatch_id,
        "_source": "transcript_parser",
    }
