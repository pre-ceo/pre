"""cache.py — decision verdict 缓存.

覆盖:
  - cache_key 确定性 + 同输入同 key
  - cache_key 受影响字段: Bash command, Read/Write/Edit file_path, Grep/Glob pattern+path
  - get_cached 未命中 / 命中 / TTL 过期
  - set_cached 创建目录 + 写文件
  - 缓存条目超过 max_entries 触发 LRU-ish 清理 (按 ts 旧→新)
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

import pytest

from cache import cache_key, get_cached, set_cached


def test_cache_key_deterministic():
    k1 = cache_key("Bash", {"command": "ls -la"})
    k2 = cache_key("Bash", {"command": "ls -la"})
    assert k1 == k2
    assert len(k1) == 16  # sha256[:16]


def test_cache_key_differs_by_command():
    a = cache_key("Bash", {"command": "ls"})
    b = cache_key("Bash", {"command": "rm -rf /"})
    assert a != b


def test_cache_key_ignores_unrelated_fields_for_bash():
    """description 等动态字段不应改 key (决策只取决于 command)."""
    a = cache_key("Bash", {"command": "ls", "description": "list"})
    b = cache_key("Bash", {"command": "ls", "description": "另一个 description"})
    assert a == b


def test_cache_key_read_uses_file_path():
    a = cache_key("Read", {"file_path": "/tmp/a.txt"})
    b = cache_key("Read", {"file_path": "/tmp/b.txt"})
    assert a != b


def test_cache_key_grep_uses_pattern_and_path():
    a = cache_key("Grep", {"pattern": "foo", "path": "/tmp"})
    b = cache_key("Grep", {"pattern": "bar", "path": "/tmp"})
    c = cache_key("Grep", {"pattern": "foo", "path": "/other"})
    assert a != b
    assert a != c


def test_cache_key_unknown_tool_hashes_full_input():
    a = cache_key("Weird", {"x": 1, "y": 2})
    b = cache_key("Weird", {"y": 2, "x": 1})  # sort_keys 保证一致
    c = cache_key("Weird", {"x": 1, "y": 3})
    assert a == b
    assert a != c


def test_get_cached_miss_when_no_file(tmp_path):
    assert get_cached(str(tmp_path), "deadbeef") is None


def test_set_and_get_roundtrip(tmp_path):
    set_cached(str(tmp_path), "key1", "allow", "safe-prefix")
    got = get_cached(str(tmp_path), "key1")
    assert got == ("allow", "safe-prefix")


def test_get_cached_expires_after_ttl(tmp_path, monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])
    set_cached(str(tmp_path), "key-ttl", "ask", "danger")
    # 立即取 — 命中
    assert get_cached(str(tmp_path), "key-ttl", ttl=10) == ("ask", "danger")
    # 时间推到 TTL 外
    fake_now[0] += 11
    assert get_cached(str(tmp_path), "key-ttl", ttl=10) is None


def test_get_cached_handles_corrupt_json(tmp_path):
    """损坏的 cache 文件不应抛, 返 None."""
    cache_file = tmp_path / "decision_cache.json"
    cache_file.write_text("{not json")
    assert get_cached(str(tmp_path), "anykey") is None


def test_set_cached_creates_missing_dir(tmp_path):
    nested = tmp_path / "deep" / "nested"
    set_cached(str(nested), "k", "allow", "")
    assert (nested / "decision_cache.json").is_file()


def test_set_cached_evicts_oldest_when_overflow(tmp_path, monkeypatch):
    """超过 500 条触发清理, 最旧的应被踢."""
    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])

    # 写 501 条, ts 单调递增
    for i in range(501):
        fake_now[0] += 1
        set_cached(str(tmp_path), f"k{i}", "allow", "")

    with open(tmp_path / "decision_cache.json") as f:
        cache = json.load(f)
    assert len(cache) == 500
    # 最早的 k0 被踢, 最新的 k500 还在
    assert "k0" not in cache
    assert "k500" in cache
