"""
pre 决策缓存
相同操作复用 governor 的上次决策, 避免重复查询
缓存按 session 隔离, 支持 TTL 过期
"""
import json
import os
import hashlib
import time


def cache_key(tool_name: str, tool_input: dict) -> str:
    """
    生成缓存 key: tool_name + 关键字段的 hash
    不含动态值 (timestamp, description 等), 只取决定安全性的字段
    """
    parts = [tool_name]

    if tool_name == "Bash":
        parts.append(tool_input.get("command", ""))
    elif tool_name in ("Read", "Write", "Edit"):
        parts.append(tool_input.get("file_path", ""))
    elif tool_name == "Grep":
        parts.append(tool_input.get("pattern", ""))
        parts.append(tool_input.get("path", ""))
    elif tool_name == "Glob":
        parts.append(tool_input.get("pattern", ""))
        parts.append(tool_input.get("path", ""))
    else:
        # 其他工具: 用完整 input 的 hash
        parts.append(json.dumps(tool_input, sort_keys=True, ensure_ascii=False))

    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_cached(agent_dir: str, key: str, ttl: int = 3600) -> tuple:
    """
    查询缓存

    Args:
        agent_dir: agent 的独立目录
        key: cache_key() 生成的 key
        ttl: 缓存有效期 (秒), 默认 1 小时

    Returns:
        (decision, reason) 或 None (未命中)
    """
    cache_file = os.path.join(agent_dir, "decision_cache.json")
    if not os.path.exists(cache_file):
        return None

    try:
        with open(cache_file) as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    entry = cache.get(key)
    if not entry:
        return None

    # TTL 检查
    if time.time() - entry.get("ts", 0) > ttl:
        return None

    return (entry["decision"], entry.get("reason", ""))


def set_cached(agent_dir: str, key: str, decision: str, reason: str):
    """写入缓存"""
    cache_file = os.path.join(agent_dir, "decision_cache.json")
    os.makedirs(agent_dir, exist_ok=True)

    cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError):
            cache = {}

    cache[key] = {
        "decision": decision,
        "reason": reason,
        "ts": time.time(),
    }

    # 限制缓存条目数, 超过时清理最旧的
    max_entries = 500
    if len(cache) > max_entries:
        sorted_keys = sorted(cache.keys(), key=lambda k: cache[k].get("ts", 0))
        for old_key in sorted_keys[:len(cache) - max_entries]:
            del cache[old_key]

    with open(cache_file, "w") as f:
        json.dump(cache, f, ensure_ascii=False)
