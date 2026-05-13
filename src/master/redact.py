"""
sensitive_patterns — SENSITIVE_PATTERNS 6 类 regex 脱敏 ( + 子条款 b).

用途:
  notify_abstract._write_audit 写 mobile_audit jsonl 时, text_preview 必经
  redact_for_audit() 脱敏 + 截 ≤100 char. audit 文件本身就不含敏感字段.

  endpoint /api/v1/notify/audit 读 audit 时直接返 text_preview (已脱敏).

API:
  redact(text) -> (sanitized, matched_patterns)
    matched_patterns: dict[pattern_name, count]
  redact_for_audit(text, max_len=100) -> (sanitized_truncated, matched_patterns)

 引入.
HC-PRE-1 stdlib only. agent-security 7 天后 data-driven 调 regex (advisory A4).
"""
from __future__ import annotations
import re
from typing import Optional


# 6 类 SENSITIVE_PATTERNS (agent-security M2 verdict, )
# (pattern_name, compiled regex, redact_placeholder)
_SENSITIVE_PATTERNS: list[tuple[str, "re.Pattern", str]] = [
    # 1. AWS Access Key ID
    ("aws_key", re.compile(r"AKIA[A-Z0-9]{16}"), "[AWS_KEY]"),
    # 2. OAuth Bearer token (header 或自由文本)
    ("oauth_bearer",
     re.compile(r"Bearer\s+[A-Za-z0-9_\-\.~+/=]{20,}", re.IGNORECASE),
     "[BEARER_TOKEN]"),
    # 3. sk-* prefix (OpenAI / Anthropic / 其他 LLM key 通配)
    ("sk_prefix", re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[SK_KEY]"),
    # 4. UUID 8-4-4-4-12 (NILSAPN_GROUP_ID 模式; 也覆盖泛 UUID 隐私 ID)
    ("uuid",
     re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
     "[UUID]"),
    # 5. Private key block (含 BEGIN ... PRIVATE KEY -- DSA/RSA/ECDSA/OPENSSH 通用)
    ("private_key",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
     "[PRIVATE_KEY_BLOCK]"),
    # 6. SSH key path / sensitive path
    ("ssh_key_path",
     re.compile(
         r"(~/?|/(home|root)/[^/\s]+/|/etc/)?(\.)?ssh/[a-zA-Z0-9_\-\.]+|"
         r"\b(id_rsa|id_ed25519|id_ecdsa|id_dsa)\b"
     ),
     "[SSH_KEY_PATH]"),
    # 7. OAuth code (claude auth login flow + 通用 OAuth callback)
    # G2: 010+011 数据驱动证明 6 类不够,
    # claude auth login 场景实证. URL fragment ?code=... 或终端 raw paste.
    ("oauth_code",
     re.compile(
         r"(?:[?&]code=|claude\s+auth\s+login.{0,50}?code[=:]\s*|"
         r"\boauth[_\-]?code[=:]\s*)([A-Za-z0-9_\-\.]{20,})",
         re.IGNORECASE
     ),
     "[OAUTH_CODE]"),
]


def redact(text: str) -> tuple[str, dict]:
    """主脱敏入口. 返 (sanitized_text, matched_patterns).
    matched_patterns: {pattern_name: count} 各类命中次数 (audit log 落).
    """
    if not isinstance(text, str) or not text:
        return text or "", {}
    sanitized = text
    matched: dict[str, int] = {}
    for name, pattern, placeholder in _SENSITIVE_PATTERNS:
        # 用 finditer 计数 + sub 替换
        count = 0
        def _sub(m, _name=name):
            nonlocal count
            count += 1
            return placeholder
        sanitized = pattern.sub(_sub, sanitized)
        if count > 0:
            matched[name] = count
    return sanitized, matched


def redact_for_audit(text: str, max_len: int = 100) -> tuple[str, dict]:
    """脱敏 + 截 max_len. 用于 mobile_audit text_preview 字段.
    agent-security M2 顺序: redact 在 truncate 之前 (防截断让 regex miss).
    """
    if not isinstance(text, str) or not text:
        return "", {}
    sanitized, matched = redact(text)
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len]
    return sanitized, matched


def list_pattern_names() -> list[str]:
    """SENSITIVE_PATTERNS 名字列表 (调试 / data-driven 调整时用)."""
    return [name for name, _, _ in _SENSITIVE_PATTERNS]


def safe_audit_dump(entry: dict) -> str:
    """[M1 spec A]: 安全 dump audit entry — 对字符串字段 redact 后 json.dumps.

    audit jsonl 全集统一调此函数替代原 json.dumps(entry), 保 master.db / log 永留时
    不含 raw 敏感数据 (HC-PRE-2 fail-safe + HC-G4 痕迹保留 + agent-security M1 P0 spec A).

    嵌套一层 dict 也 redact (audit entry 经常 含 nested context dict).
    redact 失败时 fallback 普通 dump (fail-safe 不阻 audit).
    """
    import json as _j
    if not isinstance(entry, dict):
        return _j.dumps(entry, ensure_ascii=False)
    try:
        sanitized = {}
        for k, v in entry.items():
            if isinstance(v, str) and v:
                s, _ = redact(v)
                sanitized[k] = s
            elif isinstance(v, dict):
                inner = {}
                for ik, iv in v.items():
                    if isinstance(iv, str) and iv:
                        s, _ = redact(iv)
                        inner[ik] = s
                    else:
                        inner[ik] = iv
                sanitized[k] = inner
            elif isinstance(v, list):
                inner_l = []
                for item in v:
                    if isinstance(item, str) and item:
                        s, _ = redact(item)
                        inner_l.append(s)
                    elif isinstance(item, dict):
                        inner_d = {}
                        for ik, iv in item.items():
                            if isinstance(iv, str) and iv:
                                s, _ = redact(iv)
                                inner_d[ik] = s
                            else:
                                inner_d[ik] = iv
                        inner_l.append(inner_d)
                    else:
                        inner_l.append(item)
                sanitized[k] = inner_l
            else:
                sanitized[k] = v
        return _j.dumps(sanitized, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — fail-safe
        return _j.dumps(entry, ensure_ascii=False)
