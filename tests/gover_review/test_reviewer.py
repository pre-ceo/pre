"""reviewer.py — codex review 单测.

覆盖:
  _build_prompt          含 cmd/cwd/reason/邻居/transcript/layer 约束
  _strip_code_fence      ```json ... ``` 剥离
  _parse_proposal        valid / fence / 非法 layer/action / 非 json / 非 dict / 缺字段
  _fallback_keep_ask     schema 齐全 + layer=B + action=keep_ask
  review_ask runner注入  success / rc!=0 / timeout / codex missing
  review_ask prompt 传递 cmd/rules.py 节选/rules.md 都进 prompt
  review_batch           成功/失败混合, 失败不影响其他
"""
from __future__ import annotations

import json

from gover_review.reviewer import (
    PROPOSAL_FIELDS,
    VALID_ACTIONS,
    VALID_LAYERS,
    _build_prompt,
    _fallback_keep_ask,
    _parse_proposal,
    _strip_code_fence,
    review_ask,
    review_batch,
)


def _ask(**over):
    base = {
        "ts": "2026-05-17T10:00:00+00:00",
        "session": "abc",
        "tool": "Bash",
        "cwd": "/Users/x/cursor/pre",
        "cmd": "npm install foo",
        "reason": "supply chain review",
        "source": "governor_no_cache",
        "neighbor_jsonl": [],
        "transcript_excerpt": [],
    }
    base.update(over)
    return base


def _good_proposal():
    return {
        "ask_pattern": "npm install <pkg>",
        "original_reason": "supply chain review",
        "target_layer": "C",
        "action": "whitelist",
        "rule_patch_draft": "+ 'npm install',\n",
        "user_question": "把 npm install 加白名单?",
        "risk_note": "可能装恶意包",
    }


# ---------- _build_prompt ----------

def test_build_prompt_contains_core_fields():
    p = _build_prompt(_ask())
    assert "npm install foo" in p
    assert "/Users/x/cursor/pre" in p
    assert "supply chain review" in p
    assert "governor_no_cache" in p


def test_build_prompt_neighbors_formatted():
    ask = _ask(
        neighbor_jsonl=[
            {
                "ts": "2026-05-17T10:01:00+00:00",
                "decision": "allow",
                "tool": "Bash",
                "input": {"command": "ls -la"},
            }
        ]
    )
    p = _build_prompt(ask)
    assert "ls -la" in p


def test_build_prompt_transcript_formatted():
    ask = _ask(
        transcript_excerpt=[
            {
                "timestamp": "2026-05-17T10:00:30Z",
                "type": "user",
                "message": "请装 foo",
            }
        ]
    )
    p = _build_prompt(ask)
    assert "请装 foo" in p


def test_build_prompt_lists_layer_constraints():
    p = _build_prompt(_ask())
    assert "Layer C" in p
    assert "Layer B" in p
    assert "cmd 字符串模式" in p
    assert "看不到" in p


def test_build_prompt_includes_rules_excerpt():
    p = _build_prompt(
        _ask(),
        rules_py_excerpt="_BASH_SAFE_PREFIXES = [...]",
        rules_md="# rules\n禁 rm -rf",
    )
    assert "_BASH_SAFE_PREFIXES" in p
    assert "禁 rm -rf" in p


def test_build_prompt_truncates_excerpts():
    long_rules = "x" * 5000
    p = _build_prompt(_ask(), rules_py_excerpt=long_rules, rules_md=long_rules)
    # 不应原样塞进, 必须被截
    assert long_rules not in p


# ---------- _strip_code_fence ----------

def test_strip_code_fence_json_label():
    assert _strip_code_fence('```json\n{"a":1}\n```') == '{"a":1}'


def test_strip_code_fence_no_label():
    assert _strip_code_fence('```\n{"a":1}\n```') == '{"a":1}'


def test_strip_code_fence_passthrough_when_no_fence():
    assert _strip_code_fence('{"a":1}') == '{"a":1}'


# ---------- _parse_proposal ----------

def test_parse_proposal_valid_json():
    out = _parse_proposal(json.dumps(_good_proposal()), _ask())
    assert out["action"] == "whitelist"
    assert out["target_layer"] == "C"
    for f in PROPOSAL_FIELDS:
        assert f in out


def test_parse_proposal_strips_code_fence():
    raw = "```json\n" + json.dumps(_good_proposal()) + "\n```"
    out = _parse_proposal(raw, _ask())
    assert out["action"] == "whitelist"


def test_parse_proposal_invalid_layer_falls_to_B():
    p = _good_proposal()
    p["target_layer"] = "Z"
    out = _parse_proposal(json.dumps(p), _ask())
    assert out["target_layer"] == "B"


def test_parse_proposal_invalid_action_falls_to_keep_ask():
    p = _good_proposal()
    p["action"] = "explode"
    out = _parse_proposal(json.dumps(p), _ask())
    assert out["action"] == "keep_ask"


def test_parse_proposal_non_json_fallback():
    out = _parse_proposal("not json at all", _ask(cmd="weird"))
    assert out["action"] == "keep_ask"
    assert "parse error" in out["risk_note"]
    assert out["ask_pattern"] == "weird"


def test_parse_proposal_non_dict_fallback():
    out = _parse_proposal('["not", "dict"]', _ask())
    assert out["action"] == "keep_ask"
    assert "non-dict" in out["risk_note"]


def test_parse_proposal_empty_fallback():
    out = _parse_proposal("   ", _ask())
    assert out["action"] == "keep_ask"


def test_parse_proposal_missing_fields_filled():
    raw = '{"action": "whitelist", "target_layer": "C"}'
    out = _parse_proposal(raw, _ask())
    for f in PROPOSAL_FIELDS:
        assert f in out


# ---------- _fallback_keep_ask ----------

def test_fallback_keep_ask_full_schema():
    out = _fallback_keep_ask(_ask(cmd="rm -rf /tmp"), "codex missing")
    assert out["action"] == "keep_ask"
    assert out["target_layer"] == "B"
    assert "rm -rf /tmp" in out["ask_pattern"]
    for f in PROPOSAL_FIELDS:
        assert f in out


def test_fallback_keep_ask_truncates_long_cmd():
    long_cmd = "a" * 500
    out = _fallback_keep_ask(_ask(cmd=long_cmd), "x")
    assert len(out["ask_pattern"]) <= 200


# ---------- review_ask (injected runner) ----------

def test_review_ask_success():
    raw = json.dumps(_good_proposal())
    out = review_ask(_ask(), runner=lambda p: (0, raw, ""))
    assert out["action"] == "whitelist"
    assert out["target_layer"] == "C"


def test_review_ask_nonzero_rc_fallback():
    out = review_ask(_ask(), runner=lambda p: (1, "", "boom"))
    assert out["action"] == "keep_ask"
    assert "rc=1" in out["risk_note"]
    assert "boom" in out["risk_note"]


def test_review_ask_timeout_fallback():
    out = review_ask(_ask(), runner=lambda p: (124, "", "timeout after 90s"))
    assert out["action"] == "keep_ask"
    assert "124" in out["risk_note"]


def test_review_ask_codex_missing_fallback():
    out = review_ask(
        _ask(), runner=lambda p: (127, "", "codex not found: nope")
    )
    assert out["action"] == "keep_ask"
    assert "127" in out["risk_note"]


def test_review_ask_passes_context_to_runner():
    captured: dict = {}

    def runner(p):
        captured["prompt"] = p
        return (0, json.dumps(_good_proposal()), "")

    review_ask(
        _ask(cmd="curl example.com"),
        rules_py_excerpt="_BASH_SAFE_PREFIXES",
        rules_md="# rules",
        runner=runner,
    )
    assert "curl example.com" in captured["prompt"]
    assert "_BASH_SAFE_PREFIXES" in captured["prompt"]
    assert "# rules" in captured["prompt"]


# ---------- review_batch ----------

def test_review_batch_mixed_success_failure():
    good = json.dumps(_good_proposal())

    def runner(p):
        if "first" in p:
            return (0, good, "")
        return (1, "", "fail second")

    asks = [_ask(cmd="first"), _ask(cmd="second")]
    out = review_batch(asks, runner=runner)
    assert len(out) == 2
    assert out[0]["action"] == "whitelist"
    assert out[1]["action"] == "keep_ask"
    assert "fail second" in out[1]["risk_note"]


def test_review_batch_empty():
    assert review_batch([], runner=lambda p: (0, "", "")) == []


def test_review_batch_preserves_order():
    def runner(p):
        cmd_n = "1" if "first" in p else ("2" if "second" in p else "3")
        prop = _good_proposal()
        prop["ask_pattern"] = f"n{cmd_n}"
        return (0, json.dumps(prop), "")

    asks = [_ask(cmd="first"), _ask(cmd="second"), _ask(cmd="third")]
    out = review_batch(asks, runner=runner)
    assert [p["ask_pattern"] for p in out] == ["n1", "n2", "n3"]


# ---------- 常量 sanity ----------

def test_valid_layers_constant():
    assert set(VALID_LAYERS) == {"B", "C"}


def test_valid_actions_constant():
    assert set(VALID_ACTIONS) == {
        "whitelist",
        "add_rule",
        "update_rules_md",
        "keep_ask",
    }
