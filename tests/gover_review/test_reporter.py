"""reporter.py — INFO finding 协议 + 回答 watcher + 报告单测.

覆盖:
  compute_sha256          稳定 / 内容变化 → sha 变
  format_finding          含 cycle/时间窗/Q+A 段/patch fence
  write_finding           写到 workdir/pre/findings/ + 返 sha
  parse_answers           accept/reject/skip/modify / 大小写 / 多 A 段 / 部分填 / 注释跳 / Q 段内 accept 不误识 / 第一行优先
  is_user_answered        sha 变 → True / 缺文件 → False / sha 不变 → False
  wait_for_user_answer    sha 不变 polling / sha 变拿答案 / timeout → None
  _classify_answer        分类正确
  format_report           summary 计数 / accept+modify 进 checklist / 无 accept 提示
  write_report            写到 dev-workflow/findings/YYMMDD-cycle-N.md
  move_to_processed       移到 processed/
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gover_review.reporter import (
    DEFAULT_POLL_SECONDS,
    _classify_answer,
    compute_sha256,
    format_finding,
    format_report,
    is_user_answered,
    move_to_processed,
    parse_answers,
    wait_for_user_answer,
    write_finding,
    write_report,
)


def _proposal(**over):
    base = {
        "ask_pattern": "npm install <pkg>",
        "original_reason": "supply chain review",
        "target_layer": "C",
        "action": "whitelist",
        "rule_patch_draft": "+ 'npm install',\n",
        "user_question": "把 npm install 加白名单?",
        "risk_note": "可能装恶意包",
    }
    base.update(over)
    return base


# ---------- compute_sha256 ----------

def test_compute_sha256_stable(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("hello")
    s1 = compute_sha256(p)
    s2 = compute_sha256(p)
    assert s1 == s2
    assert len(s1) == 64


def test_compute_sha256_changes_on_edit(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("hello")
    s1 = compute_sha256(p)
    p.write_text("hello!")
    assert compute_sha256(p) != s1


# ---------- format_finding ----------

def test_format_finding_has_header_and_window():
    out = format_finding(4, "2026-05-17T08:00:00+00:00", "2026-05-17T12:00:00+00:00", [_proposal()])
    assert "cycle 4" in out
    assert "2026-05-17T08:00:00+00:00" in out
    assert "2026-05-17T12:00:00+00:00" in out


def test_format_finding_has_q_and_a_per_proposal():
    out = format_finding(1, "s", "u", [_proposal(), _proposal(ask_pattern="curl x")])
    assert "### Q1:" in out
    assert "### A1" in out
    assert "### Q2:" in out
    assert "### A2" in out


def test_format_finding_includes_patch_in_fence():
    out = format_finding(1, "s", "u", [_proposal(rule_patch_draft="+ FOO\n- BAR")])
    assert "```diff" in out
    assert "+ FOO" in out
    assert "- BAR" in out


def test_format_finding_empty_patch_placeholder():
    out = format_finding(1, "s", "u", [_proposal(rule_patch_draft="")])
    assert "(empty" in out


# ---------- write_finding ----------

def test_write_finding_creates_file_and_returns_sha(tmp_path):
    wd = tmp_path / "wd"
    path, sha = write_finding(wd, 4, "s", "u", [_proposal()])
    assert path.exists()
    assert path.name == "INFO-gover-improve-cycle-4.md"
    assert path.parent == wd / "pre" / "findings"
    assert sha == compute_sha256(path)


# ---------- parse_answers ----------

def test_parse_answers_basic_four_types(tmp_path):
    f = tmp_path / "x.md"
    f.write_text(
        "### A1\naccept\n"
        "### A2\nreject\n"
        "### A3\nskip\n"
        "### A4\nmodify: 改成只允许 cwd 内\n"
    )
    out = parse_answers(f)
    assert out[1] == "accept"
    assert out[2] == "reject"
    assert out[3] == "skip"
    assert out[4].startswith("modify:")


def test_parse_answers_case_insensitive(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("### A1\nAccept\n### A2\nREJECT\n")
    out = parse_answers(f)
    assert out[1].lower() == "accept"
    assert out[2].lower() == "reject"


def test_parse_answers_partial_only_filled(tmp_path):
    f = tmp_path / "x.md"
    f.write_text(
        "### A1\naccept\n\n"
        "### A2\n<!-- 还没填 -->\n\n"
        "### A3\nskip\n"
    )
    out = parse_answers(f)
    assert 1 in out and 3 in out
    assert 2 not in out


def test_parse_answers_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "x.md"
    f.write_text(
        "### A1\n\n<!-- comment -->\n  \naccept\n"
    )
    out = parse_answers(f)
    assert out[1] == "accept"


def test_parse_answers_first_match_wins(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("### A1\naccept\nskip\nreject\n")
    out = parse_answers(f)
    assert out[1] == "accept"


def test_parse_answers_ignores_q_section_text(tmp_path):
    """Q 段里 user_question 含 'accept?' 字串不能被误识为答案."""
    f = tmp_path / "x.md"
    f.write_text(
        "### Q1: foo\nuser_question: 是否 accept?\n\n"
        "### A1\nreject\n"
    )
    out = parse_answers(f)
    assert out[1] == "reject"  # 不是 accept


def test_parse_answers_a_section_ends_at_next_heading(tmp_path):
    """A 段在遇到下一个 ### 时结束, 不读到下个段的答案."""
    f = tmp_path / "x.md"
    f.write_text(
        "### A1\n\n### Q2: foo\naccept\n### A2\nreject\n"
    )
    out = parse_answers(f)
    assert 1 not in out  # A1 没填
    assert out[2] == "reject"


def test_parse_answers_strict_anchor_not_substring(tmp_path):
    """'这条 accept' 不算 (没行级 anchor)."""
    f = tmp_path / "x.md"
    f.write_text("### A1\n这条 accept 这个\n### A2\nreject\n")
    out = parse_answers(f)
    assert 1 not in out
    assert out[2] == "reject"


def test_parse_answers_empty_file(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("")
    assert parse_answers(f) == {}


# ---------- is_user_answered ----------

def test_is_user_answered_sha_unchanged_false(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("hello")
    sha = compute_sha256(f)
    assert is_user_answered(f, sha) is False


def test_is_user_answered_sha_changed_true(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("hello")
    sha = compute_sha256(f)
    f.write_text("hello edited")
    assert is_user_answered(f, sha) is True


def test_is_user_answered_missing_file_false(tmp_path):
    assert is_user_answered(tmp_path / "nope.md", "abc") is False


# ---------- wait_for_user_answer ----------

def test_wait_for_user_answer_returns_when_sha_changes(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("### A1\n")
    sha = compute_sha256(f)

    poll_count = {"n": 0}

    def sleeper(s):
        poll_count["n"] += 1
        if poll_count["n"] == 3:  # 第 3 次 poll 时模拟用户编辑
            f.write_text("### A1\naccept\n")

    out = wait_for_user_answer(
        f, sha, poll_seconds=0.01, sleeper=sleeper
    )
    assert out == {1: "accept"}
    assert poll_count["n"] >= 3


def test_wait_for_user_answer_timeout_returns_none(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("### A1\n")
    sha = compute_sha256(f)

    # mock clock 让 timeout 立即生效
    fake_time = {"t": 0.0}

    def clock():
        return fake_time["t"]

    def sleeper(s):
        fake_time["t"] += s

    out = wait_for_user_answer(
        f,
        sha,
        poll_seconds=10.0,
        timeout_seconds=20.0,
        sleeper=sleeper,
        clock=clock,
    )
    assert out is None


def test_wait_for_user_answer_immediate_return_if_sha_already_changed(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("### A1\naccept\n")
    sha = "different_sha"  # 假装 original sha 跟当前不同 (= 已有答案)

    # sleeper 不应被调
    def sleeper(s):
        pytest.fail("sleeper should not be called")

    out = wait_for_user_answer(f, sha, sleeper=sleeper)
    assert out == {1: "accept"}


# ---------- _classify_answer ----------

def test_classify_answer_buckets():
    assert _classify_answer("accept") == "accept"
    assert _classify_answer("REJECT") == "reject"
    assert _classify_answer(" Skip ") == "skip"
    assert _classify_answer("modify: 改") == "modify"
    assert _classify_answer("") == "no_answer"
    assert _classify_answer("garbage") == "no_answer"


# ---------- format_report ----------

def test_format_report_summary_counts_correctly():
    proposals = [_proposal(), _proposal(), _proposal(), _proposal()]
    answers = {1: "accept", 2: "reject", 3: "modify: x", 4: "skip"}
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    out = format_report(1, "s", "u", proposals, answers, now=now)
    assert "accept: 1" in out
    assert "reject: 1" in out
    assert "modify: 1" in out
    assert "skip: 1" in out
    assert "no_answer: 0" in out


def test_format_report_no_answer_when_missing():
    proposals = [_proposal()]
    out = format_report(1, "s", "u", proposals, {}, now=datetime(2026, 5, 17, tzinfo=timezone.utc))
    assert "no_answer: 1" in out


def test_format_report_checklist_only_accept_and_modify():
    proposals = [
        _proposal(target_layer="C"),
        _proposal(target_layer="B"),
        _proposal(target_layer="C"),
        _proposal(target_layer="B"),
    ]
    answers = {1: "accept", 2: "reject", 3: "modify: x", 4: "skip"}
    out = format_report(1, "s", "u", proposals, answers, now=datetime(2026, 5, 17, tzinfo=timezone.utc))
    assert "apply #1" in out
    assert "apply #3" in out
    assert "apply #2" not in out  # reject 不进 checklist
    assert "apply #4" not in out  # skip 不进 checklist


def test_format_report_no_checklist_when_no_accept():
    proposals = [_proposal()]
    out = format_report(1, "s", "u", proposals, {1: "reject"}, now=datetime(2026, 5, 17, tzinfo=timezone.utc))
    assert "无需 apply" in out


def test_format_report_target_layer_hint():
    proposals = [_proposal(target_layer="C"), _proposal(target_layer="B")]
    answers = {1: "accept", 2: "accept"}
    out = format_report(1, "s", "u", proposals, answers, now=datetime(2026, 5, 17, tzinfo=timezone.utc))
    assert "src/rules.py" in out
    assert "rules.md" in out


# ---------- write_report ----------

def test_write_report_writes_to_dev_workflow_findings(tmp_path):
    fdir = tmp_path / "dev-workflow" / "findings"
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    p = write_report(fdir, 4, "s", "u", [_proposal()], {1: "accept"}, now=now)
    assert p.exists()
    assert p.name == "260517-cycle-4.md"
    assert "改进报告" in p.read_text()


# ---------- move_to_processed ----------

def test_move_to_processed_relocates(tmp_path):
    fdir = tmp_path / "findings"
    fdir.mkdir()
    src = fdir / "INFO-gover-improve-cycle-4.md"
    src.write_text("hello")
    dst = move_to_processed(src)
    assert dst == fdir / "processed" / "INFO-gover-improve-cycle-4.md"
    assert dst.exists()
    assert not src.exists()


# ---------- end-to-end ----------

def test_full_cycle_write_finding_user_edits_parse_report(tmp_path):
    """端到端: 写 finding → 模拟用户编辑 → polling 拿答案 → 写报告 → 移 processed."""
    wd = tmp_path / "wd"
    proposals = [
        _proposal(target_layer="C", ask_pattern="npm install <x>"),
        _proposal(target_layer="B", ask_pattern="curl <host>", rule_patch_draft=""),
    ]
    fpath, sha = write_finding(wd, 7, "2026-05-17T08:00:00+00:00", "2026-05-17T12:00:00+00:00", proposals)

    assert is_user_answered(fpath, sha) is False
    fpath.write_text(fpath.read_text() + "\n\n# user edited\n### A1\naccept\n### A2\nreject\n")
    assert is_user_answered(fpath, sha) is True

    answers = parse_answers(fpath)
    # 多 A1 段 → 第一个 (原模板里 A1 是空的) 取不到, 第二个 (用户加的) 应该是 'accept'
    # 简化: 只看 A2 = reject (原 A2 也是空, 用户加的 A2=reject)
    # 实际行为: parse 找到第一个 A1 在 line ~20, 段是空, 然后 ### A2 段也空, 然后用户加的 ### A1 又 reset, accept
    # 因 parse 遇到 "### A1" 重新 set current_n=1
    assert answers.get(1) == "accept"
    assert answers.get(2) == "reject"

    rdir = tmp_path / "dev-workflow" / "findings"
    rpath = write_report(
        rdir, 7,
        "2026-05-17T08:00:00+00:00", "2026-05-17T12:00:00+00:00",
        proposals, answers,
        now=datetime(2026, 5, 17, 13, tzinfo=timezone.utc),
    )
    assert rpath.exists()
    assert "accept: 1" in rpath.read_text()
    assert "reject: 1" in rpath.read_text()

    moved = move_to_processed(fpath)
    assert moved.exists()
    assert not fpath.exists()
