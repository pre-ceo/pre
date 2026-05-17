"""ask 条目抽取 + 上下文打包 (Layer A 数据采集).

输入:
  log_dir              pre_rule/logs/ 含 pre_hook_YYYYMMDD.jsonl
  since, until         UTC datetime, 半开区间 [since, until)
  claude_projects_dir  ~/.claude/projects (可选, 反查 transcript)

输出:
  {
    "since": ISO, "until": ISO, "n_ask": int,
    "ask_entries": [{
      ts, session, tool, cwd, cmd, tool_input, reason, source, agent_dir,
      neighbor_jsonl: [...],         # 同 session + cwd 窗内 (不含自身)
      transcript_excerpt: [...],     # ts 前后 N 条 (按 Claude Code jsonl)
    }]
  }

筛选条件:
  decision == "ask"
  source ∈ {"governor", "governor_no_cache"}   # cache/local 不在重审范围
  since <= ts < until
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_WINDOW_SECONDS = 300
DEFAULT_TRANSCRIPT_N = 10
ASK_SOURCES = ("governor", "governor_no_cache")


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(s2)
    except ValueError:
        return None


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-")


def _extract_cmd(entry: dict) -> str:
    tool = entry.get("tool", "")
    inp = entry.get("input") if isinstance(entry.get("input"), dict) else {}
    if tool == "Bash":
        c = inp.get("command") if inp else None
        if c:
            return str(c)
        return str(entry.get("command_preview", ""))
    if tool in ("Read", "Write", "Edit"):
        fp = (inp.get("file_path") if inp else None) or entry.get("file_path", "")
        return f"{tool} {fp}".strip()
    if tool in ("Grep", "Glob"):
        pat = (inp.get("pattern") if inp else None) or entry.get("pattern", "")
        return f"{tool} {pat}".strip()
    if tool == "Agent":
        desc = (inp.get("description") if inp else None) or entry.get("description", "")
        return f"Agent {desc}".strip()
    return tool


def find_log_files(log_dir: Path, since: datetime, until: datetime) -> list[Path]:
    if since > until:
        return []
    files: list[Path] = []
    cursor = since.astimezone(timezone.utc).date()
    end = until.astimezone(timezone.utc).date()
    while cursor <= end:
        f = log_dir / f"pre_hook_{cursor.strftime('%Y%m%d')}.jsonl"
        if f.exists():
            files.append(f)
        cursor += timedelta(days=1)
    return files


def iter_entries(paths: Iterable[Path]) -> Iterator[dict]:
    for p in paths:
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def filter_ask_entries(
    entries: Iterable[dict], since: datetime, until: datetime
) -> list[dict]:
    out: list[dict] = []
    for e in entries:
        if e.get("decision") != "ask":
            continue
        if e.get("source") not in ASK_SOURCES:
            continue
        ts = _parse_ts(e.get("ts", ""))
        if ts is None or ts < since or ts >= until:
            continue
        out.append(e)
    return out


def gather_neighbors(
    all_entries: list[dict], target: dict, window_seconds: int = DEFAULT_WINDOW_SECONDS
) -> list[dict]:
    tgt_ts = _parse_ts(target.get("ts", ""))
    if tgt_ts is None:
        return []
    sess = target.get("session")
    cwd = target.get("cwd")
    delta = timedelta(seconds=window_seconds)
    out: list[dict] = []
    for e in all_entries:
        if e is target:
            continue
        if e.get("session") != sess or e.get("cwd") != cwd:
            continue
        ets = _parse_ts(e.get("ts", ""))
        if ets is None:
            continue
        if abs(ets - tgt_ts) <= delta:
            out.append(e)
    return out


def find_transcript_for(
    claude_projects_dir: Path, cwd: str, session_prefix: str, ts: datetime
) -> Path | None:
    proj_dir = claude_projects_dir / _encode_cwd(cwd)
    if not proj_dir.exists():
        return None
    if not session_prefix:
        return None
    candidates = list(proj_dir.glob(f"{session_prefix}*.jsonl"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def transcript_excerpt(
    path: Path,
    target_ts: datetime,
    n_before: int = DEFAULT_TRANSCRIPT_N,
    n_after: int = DEFAULT_TRANSCRIPT_N,
) -> list[dict]:
    entries: list[tuple[datetime, dict]] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = _parse_ts(obj.get("timestamp", ""))
                if t is None:
                    continue
                entries.append((t, obj))
    except OSError:
        return []
    if not entries:
        return []
    idx = len(entries)
    for i, (t, _) in enumerate(entries):
        if t >= target_ts:
            idx = i
            break
    lo = max(0, idx - n_before)
    hi = min(len(entries), idx + n_after)
    return [obj for _, obj in entries[lo:hi]]


def extract(
    *,
    log_dir: Path,
    since: datetime,
    until: datetime,
    claude_projects_dir: Path | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    transcript_n: int = DEFAULT_TRANSCRIPT_N,
) -> dict:
    ext_since = since - timedelta(seconds=window_seconds)
    ext_until = until + timedelta(seconds=window_seconds)
    ext_log_files = find_log_files(log_dir, ext_since, ext_until)

    # 单次解析复用对象 — gather_neighbors 用 `is` 判断自身, 必须共享 dict 引用
    all_entries = list(iter_entries(ext_log_files))
    ask_entries = filter_ask_entries(all_entries, since, until)

    out_entries: list[dict] = []
    for ae in ask_entries:
        cmd = _extract_cmd(ae)
        tool_input = ae.get("input") if isinstance(ae.get("input"), dict) else {}
        neighbors = gather_neighbors(all_entries, ae, window_seconds)
        transcript: list[dict] = []
        if claude_projects_dir is not None:
            ts = _parse_ts(ae.get("ts", ""))
            sess = ae.get("session", "")
            cwd = ae.get("cwd", "")
            if ts and sess and cwd:
                tpath = find_transcript_for(claude_projects_dir, cwd, sess, ts)
                if tpath:
                    transcript = transcript_excerpt(tpath, ts, transcript_n, transcript_n)
        out_entries.append(
            {
                "ts": ae.get("ts"),
                "session": ae.get("session"),
                "tool": ae.get("tool"),
                "cwd": ae.get("cwd"),
                "cmd": cmd,
                "tool_input": tool_input,
                "reason": ae.get("reason", ""),
                "source": ae.get("source"),
                "agent_dir": ae.get("agent_dir"),
                "neighbor_jsonl": neighbors,
                "transcript_excerpt": transcript,
            }
        )
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "n_ask": len(out_entries),
        "ask_entries": out_entries,
    }
