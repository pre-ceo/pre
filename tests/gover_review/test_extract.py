"""extract.py — ask 抽取 + 上下文打包单测.

覆盖:
  _parse_ts             双格式 (Z / +00:00) + bad input
  _encode_cwd           cwd → Claude Code 目录名编码
  _extract_cmd          Bash / Read / Edit / Grep / Agent / unknown
  find_log_files        单日 / 跨日 / 缺失文件
  iter_entries          流式 + 跳空行 + 跳坏 json
  filter_ask_entries    source 白名单 / decision / 半开区间边界
  gather_neighbors      same session+cwd / 自身排除 / 跨 session/cwd 过滤
  find_transcript_for   session 前缀匹配 / 缺失 → None
  transcript_excerpt    前 N 后 N / target 早于所有
  extract               端到端 (jsonl + transcript)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gover_review.extract import (
    DEFAULT_WINDOW_SECONDS,
    _encode_cwd,
    _extract_cmd,
    _parse_ts,
    extract,
    filter_ask_entries,
    find_log_files,
    find_transcript_for,
    gather_neighbors,
    iter_entries,
    transcript_excerpt,
)


def _entry(
    ts: str,
    decision: str,
    source: str,
    *,
    tool: str = "Bash",
    cmd: str = "echo x",
    session: str = "26888743-2f3",
    cwd: str = "/Users/x/cursor/pre",
) -> dict:
    return {
        "ts": ts,
        "tool": tool,
        "mode": "enforce",
        "cwd": cwd,
        "session": session,
        "input": {"command": cmd},
        "decision": decision,
        "reason": "...",
        "source": source,
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------- _parse_ts ----------

def test_parse_ts_offset_and_z_equivalent():
    a = _parse_ts("2026-05-17T08:00:00+00:00")
    b = _parse_ts("2026-05-17T08:00:00Z")
    assert a is not None and a == b


def test_parse_ts_invalid_returns_none():
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None
    assert _parse_ts("2026-13-99") is None


# ---------- _encode_cwd ----------

def test_encode_cwd_replaces_slashes():
    assert _encode_cwd("/Users/x/cursor/pre") == "-Users-x-cursor-pre"


# ---------- _extract_cmd ----------

def test_extract_cmd_bash_from_input():
    assert _extract_cmd(_entry("t", "ask", "governor", cmd="ls -la")) == "ls -la"


def test_extract_cmd_bash_falls_back_to_preview():
    e = {"tool": "Bash", "command_preview": "echo preview"}
    assert _extract_cmd(e) == "echo preview"


def test_extract_cmd_read_includes_path():
    e = {"tool": "Read", "input": {"file_path": "/etc/hosts"}}
    assert _extract_cmd(e) == "Read /etc/hosts"


def test_extract_cmd_grep_pattern():
    e = {"tool": "Grep", "input": {"pattern": "sk-[a-z]+"}}
    assert _extract_cmd(e) == "Grep sk-[a-z]+"


def test_extract_cmd_agent_description_top_level():
    e = {"tool": "Agent", "description": "review code"}
    assert _extract_cmd(e) == "Agent review code"


def test_extract_cmd_unknown_returns_tool():
    assert _extract_cmd({"tool": "MysteryTool"}) == "MysteryTool"


# ---------- find_log_files ----------

def test_find_log_files_single_day(tmp_path):
    log = tmp_path / "logs"
    log.mkdir()
    (log / "pre_hook_20260517.jsonl").write_text("")
    since = datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    files = find_log_files(log, since, until)
    assert [f.name for f in files] == ["pre_hook_20260517.jsonl"]


def test_find_log_files_cross_day(tmp_path):
    log = tmp_path / "logs"
    log.mkdir()
    (log / "pre_hook_20260516.jsonl").write_text("")
    (log / "pre_hook_20260517.jsonl").write_text("")
    since = datetime(2026, 5, 16, 22, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 17, 2, 0, tzinfo=timezone.utc)
    files = find_log_files(log, since, until)
    assert [f.name for f in files] == [
        "pre_hook_20260516.jsonl",
        "pre_hook_20260517.jsonl",
    ]


def test_find_log_files_missing_skipped(tmp_path):
    log = tmp_path / "logs"
    log.mkdir()
    (log / "pre_hook_20260517.jsonl").write_text("")
    since = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    files = find_log_files(log, since, until)
    assert [f.name for f in files] == ["pre_hook_20260517.jsonl"]


def test_find_log_files_inverted_range_empty(tmp_path):
    log = tmp_path / "logs"
    log.mkdir()
    later = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 5, 17, 8, 0, tzinfo=timezone.utc)
    assert find_log_files(log, later, earlier) == []


# ---------- iter_entries ----------

def test_iter_entries_skips_blank_and_bad_json(tmp_path):
    log = tmp_path / "x.jsonl"
    log.write_text('{"a": 1}\n\n{not json}\n{"b": 2}\n')
    out = list(iter_entries([log]))
    assert out == [{"a": 1}, {"b": 2}]


def test_iter_entries_missing_file_silent(tmp_path):
    out = list(iter_entries([tmp_path / "nope.jsonl"]))
    assert out == []


# ---------- filter_ask_entries ----------

def test_filter_ask_only_governor_sources():
    since = datetime(2026, 5, 17, 8, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    entries = [
        _entry("2026-05-17T09:00:00+00:00", "ask", "governor"),
        _entry("2026-05-17T09:30:00+00:00", "ask", "governor_no_cache"),
        _entry("2026-05-17T09:35:00+00:00", "ask", "local"),
        _entry("2026-05-17T09:40:00+00:00", "ask", "cache"),
        _entry("2026-05-17T10:00:00+00:00", "allow", "governor"),
        _entry("2026-05-17T07:00:00+00:00", "ask", "governor"),
        _entry("2026-05-17T13:00:00+00:00", "ask", "governor"),
    ]
    out = filter_ask_entries(entries, since, until)
    assert len(out) == 2
    assert {e["source"] for e in out} == {"governor", "governor_no_cache"}


def test_filter_ask_until_exclusive_since_inclusive():
    since = datetime(2026, 5, 17, 8, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    entries = [
        _entry("2026-05-17T12:00:00+00:00", "ask", "governor"),
        _entry("2026-05-17T11:59:59+00:00", "ask", "governor"),
        _entry("2026-05-17T08:00:00+00:00", "ask", "governor"),
    ]
    out = filter_ask_entries(entries, since, until)
    assert len(out) == 2
    tss = {e["ts"] for e in out}
    assert "2026-05-17T12:00:00+00:00" not in tss


# ---------- gather_neighbors ----------

def test_gather_neighbors_same_session_and_cwd():
    target = _entry("2026-05-17T10:00:00+00:00", "ask", "governor")
    same = _entry("2026-05-17T10:02:00+00:00", "allow", "local", cmd="ls")
    far = _entry("2026-05-17T10:10:00+00:00", "allow", "local", cmd="ls -l")
    diff_sess = _entry(
        "2026-05-17T10:01:00+00:00", "allow", "local", session="xxxxxxxxxxxx"
    )
    diff_cwd = _entry(
        "2026-05-17T10:01:00+00:00",
        "allow",
        "local",
        cwd="/Users/x/cursor/other",
    )
    all_e = [target, same, far, diff_sess, diff_cwd]
    out = gather_neighbors(all_e, target, window_seconds=300)
    assert out == [same]


def test_gather_neighbors_target_excluded():
    target = _entry("2026-05-17T10:00:00+00:00", "ask", "governor")
    out = gather_neighbors([target], target, window_seconds=300)
    assert out == []


def test_gather_neighbors_window_boundary_inclusive():
    target = _entry("2026-05-17T10:00:00+00:00", "ask", "governor")
    edge = _entry("2026-05-17T10:05:00+00:00", "allow", "local")
    over = _entry("2026-05-17T10:05:01+00:00", "allow", "local")
    out = gather_neighbors([target, edge, over], target, window_seconds=300)
    assert out == [edge]


# ---------- find_transcript_for ----------

def test_find_transcript_for_matches_session_prefix(tmp_path):
    proj = tmp_path / "projects"
    cwd = "/Users/x/cursor/pre"
    pdir = proj / "-Users-x-cursor-pre"
    pdir.mkdir(parents=True)
    f = pdir / "26888743-2f30-4159-ac82-bdb1955688cb.jsonl"
    f.write_text("")
    out = find_transcript_for(
        proj, cwd, "26888743-2f3", datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
    )
    assert out == f


def test_find_transcript_for_no_proj_dir(tmp_path):
    proj = tmp_path / "projects"
    out = find_transcript_for(
        proj,
        "/Users/x/missing",
        "abc",
        datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
    )
    assert out is None


def test_find_transcript_for_empty_prefix_none(tmp_path):
    proj = tmp_path / "projects"
    pdir = proj / "-Users-x-cursor-pre"
    pdir.mkdir(parents=True)
    (pdir / "26888743-2f30.jsonl").write_text("")
    out = find_transcript_for(
        proj, "/Users/x/cursor/pre", "", datetime.now(timezone.utc)
    )
    assert out is None


# ---------- transcript_excerpt ----------

def test_transcript_excerpt_window(tmp_path):
    f = tmp_path / "session.jsonl"
    lines = []
    base = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
    for i in range(20):
        t = base + timedelta(seconds=i * 30)
        lines.append(
            json.dumps(
                {"timestamp": t.isoformat().replace("+00:00", "Z"), "uuid": f"u{i}"}
            )
        )
    f.write_text("\n".join(lines) + "\n")
    target_ts = base + timedelta(minutes=5)
    out = transcript_excerpt(f, target_ts, n_before=3, n_after=2)
    assert len(out) == 5
    assert [o["uuid"] for o in out] == ["u7", "u8", "u9", "u10", "u11"]


def test_transcript_excerpt_target_before_all_returns_head(tmp_path):
    f = tmp_path / "s.jsonl"
    t = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
    f.write_text(
        json.dumps({"timestamp": t.isoformat().replace("+00:00", "Z"), "uuid": "only"})
        + "\n"
    )
    early = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    out = transcript_excerpt(f, early, n_before=3, n_after=3)
    assert [o["uuid"] for o in out] == ["only"]


def test_transcript_excerpt_missing_file_empty(tmp_path):
    out = transcript_excerpt(
        tmp_path / "absent.jsonl", datetime.now(timezone.utc), 5, 5
    )
    assert out == []


# ---------- extract end-to-end ----------

def test_extract_end_to_end(tmp_path):
    log = tmp_path / "logs"
    log.mkdir()
    proj = tmp_path / "projects"
    pdir = proj / "-Users-x-cursor-pre"
    pdir.mkdir(parents=True)

    target_ts = "2026-05-17T10:00:00+00:00"
    neighbor_ts = "2026-05-17T10:02:00+00:00"
    out_window_ts = "2026-05-17T07:00:00+00:00"
    entries = [
        _entry(target_ts, "ask", "governor", cmd="curl example.com"),
        _entry(neighbor_ts, "allow", "local", cmd="ls"),
        _entry(out_window_ts, "ask", "governor", cmd="rm /tmp/x"),
    ]
    _write_jsonl(log / "pre_hook_20260517.jsonl", entries)

    tf = pdir / "26888743-2f30-4159-ac82-bdb1955688cb.jsonl"
    tf.write_text(
        json.dumps({"timestamp": "2026-05-17T10:00:30Z", "uuid": "ok"}) + "\n"
    )

    since = datetime(2026, 5, 17, 8, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    out = extract(log_dir=log, since=since, until=until, claude_projects_dir=proj)

    assert out["n_ask"] == 1
    ent = out["ask_entries"][0]
    assert ent["cmd"] == "curl example.com"
    assert len(ent["neighbor_jsonl"]) == 1
    assert ent["neighbor_jsonl"][0]["ts"] == neighbor_ts
    assert len(ent["transcript_excerpt"]) == 1
    assert ent["transcript_excerpt"][0]["uuid"] == "ok"


def test_extract_no_transcript_dir_runs_fine(tmp_path):
    log = tmp_path / "logs"
    log.mkdir()
    _write_jsonl(
        log / "pre_hook_20260517.jsonl",
        [_entry("2026-05-17T10:00:00+00:00", "ask", "governor")],
    )
    since = datetime(2026, 5, 17, 8, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    out = extract(log_dir=log, since=since, until=until, claude_projects_dir=None)
    assert out["n_ask"] == 1
    assert out["ask_entries"][0]["transcript_excerpt"] == []


def test_extract_cross_day_neighbors_pulled(tmp_path):
    """ask 在新日开头 → ±5min 邻居跨到前一日, 必须读到前一日 jsonl"""
    log = tmp_path / "logs"
    log.mkdir()
    _write_jsonl(
        log / "pre_hook_20260516.jsonl",
        [_entry("2026-05-16T23:58:00+00:00", "allow", "local", cmd="prev-day")],
    )
    _write_jsonl(
        log / "pre_hook_20260517.jsonl",
        [_entry("2026-05-17T00:01:00+00:00", "ask", "governor", cmd="new-day")],
    )
    since = datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 17, 4, 0, tzinfo=timezone.utc)
    out = extract(
        log_dir=log,
        since=since,
        until=until,
        window_seconds=DEFAULT_WINDOW_SECONDS,
    )
    assert out["n_ask"] == 1
    neigh = out["ask_entries"][0]["neighbor_jsonl"]
    assert len(neigh) == 1
    assert neigh[0]["input"]["command"] == "prev-day"
