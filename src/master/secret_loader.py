"""
secret_loader — per-node secret 加载 + 校验.

[ step 2 / / agent-security M2 / deadline 内]

设计:
  - secret 文件: ~/.pre/secrets/<node_id>.json chmod 600
  - schema: {node_id, secret_hash sha256, created_ts, last_rotated_ts, expires_ts}
  - master 启动加载所有 node secret hash, raw secret 不存盘
  - ws/HTTP auth 双校: NODE_SECRETS[node_id].secret_hash 或 旧 PRE_SECRET 30d grace

API:
  load_node_secrets() -> dict[node_id, dict] (启动调用)
  check_node_secret(node_id, raw_token) -> tuple[bool, str] # (ok, reason)
  is_legacy_grace_active(deploy_ts: float) -> bool # 30d grace check
  generate_secret_file(node_id, raw_secret) -> Path # 生成 schema 文件 (deploy 用)

HC-PRE-1 stdlib only (hashlib + json + os).
HC-PRE-2 fail-safe (文件不存在 / 损坏 → 空 dict, fallback 旧 secret).
HC-G4 痕迹保留 (旧 secret grace 期内仍接受, 不破现 4 remote-node ws 连接).
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

_SECRETS_DIR = Path(os.environ.get(
    "PRE_SECRETS_DIR",
    str(Path.home() / ".pre" / "secrets")
))

# Grace period: 旧 PRE_SECRET 30 天内仍 accepted (从 deploy ts 起算)
_LEGACY_GRACE_SEC = 30 * 86400

# Hard expiry: 90 天硬截止
_HARD_EXPIRY_SEC = 90 * 86400

# Warning window: 7 天预警
_WARN_WINDOW_SEC = 7 * 86400

# 全局 dict, 启动加载, mtime hot reload (类似 capability)
NODE_SECRETS: dict[str, dict] = {}
_CACHE = {"mtime_max": 0.0}


def _hash_secret(raw: str) -> str:
    """sha256 hex of raw secret. master 仅持 hash, 不存明文."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_node_secrets() -> dict[str, dict]:
    """启动加载 + mtime 检查重 reload. 失败 fail-safe 返当前 cache.

    返 NODE_SECRETS dict 引用 (不是 copy, 调用方不应修改).
    """
    global NODE_SECRETS  # noqa: PLW0603
    if not _SECRETS_DIR.exists():
        return NODE_SECRETS
    try:
        mtime_max = 0.0
        new_dict: dict[str, dict] = {}
        for f in _SECRETS_DIR.glob("*.json"):
            try:
                mt = f.stat().st_mtime
                mtime_max = max(mtime_max, mt)
                with open(f, encoding="utf-8") as fp:
                    d = json.load(fp)
                node_id = d.get("node_id")
                secret_hash = d.get("secret_hash")
                if node_id and secret_hash and isinstance(secret_hash, str):
                    new_dict[node_id] = d
            except (OSError, ValueError, json.JSONDecodeError):
                continue  # fail-safe: 跳过坏文件不阻整体加载
        if mtime_max > _CACHE["mtime_max"] or len(new_dict) != len(NODE_SECRETS):
            NODE_SECRETS = new_dict
            _CACHE["mtime_max"] = mtime_max
        return NODE_SECRETS
    except OSError:
        return NODE_SECRETS  # fail-safe


def check_node_secret(node_id: str, raw_token: str) -> tuple[bool, str]:
    """校验 raw_token 跟 NODE_SECRETS[node_id].secret_hash 是否匹配.
    返 (ok, reason).

    reason 值:
      - "node_secret_match": ok, hash 匹配
      - "node_unknown": NODE_SECRETS 没此 node_id
      - "secret_mismatch": hash 不匹配
      - "secret_expired": expires_ts < now (90d 硬截止)
    """
    if not node_id or not raw_token:
        return False, "missing_input"
    entry = NODE_SECRETS.get(node_id)
    if not entry:
        return False, "node_unknown"
    expected = entry.get("secret_hash", "")
    if _hash_secret(raw_token) != expected:
        return False, "secret_mismatch"
    exp = entry.get("expires_ts", 0.0)
    if exp and exp < time.time():
        return False, "secret_expired"
    return True, "node_secret_match"


def get_secret_status(node_id: str) -> Optional[dict]:
    """返 secret status 给 cron / monitor 看. 含 expiry warning level."""
    entry = NODE_SECRETS.get(node_id)
    if not entry:
        return None
    exp = entry.get("expires_ts", 0.0)
    now = time.time()
    if not exp:
        level = "no_expiry"
    elif exp < now:
        level = "expired"
    elif exp < now + _WARN_WINDOW_SEC:
        level = "expiring_soon"  # 7d 内
    else:
        level = "valid"
    return {
        "node_id": node_id,
        "expires_ts": exp,
        "expires_in_sec": exp - now if exp else None,
        "level": level,
        "created_ts": entry.get("created_ts"),
        "last_rotated_ts": entry.get("last_rotated_ts"),
    }


def list_expiring_soon() -> list[dict]:
    """返 7d 内过期的 node 列表 (cron 用 → finding INFO/HIGH)."""
    out = []
    for node_id in NODE_SECRETS:
        s = get_secret_status(node_id)
        if s and s["level"] in ("expiring_soon", "expired"):
            out.append(s)
    return out


def generate_secret_file(node_id: str, raw_secret: str,
                          created_ts: Optional[float] = None) -> Path:
    """生成 ~/.pre/secrets/<node_id>.json (deploy 一次性用).
    raw_secret 不入文件, 仅 hash. chmod 600 + dir 700.
    """
    if not node_id or not raw_secret:
        raise ValueError("missing node_id or raw_secret")
    _SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(_SECRETS_DIR), 0o700)
    except OSError:
        pass
    now = float(created_ts or time.time())
    entry = {
        "node_id": node_id,
        "secret_hash": _hash_secret(raw_secret),
        "created_ts": now,
        "last_rotated_ts": now,
        "expires_ts": now + _HARD_EXPIRY_SEC,
        "_doc": (
            "per-node secret. raw secret 仅在 each node env, "
            "master 仅持 sha256 hash. 严禁跨 node 传 raw secret."
        ),
    }
    fpath = _SECRETS_DIR / f"{node_id}.json"
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(str(fpath), 0o600)
    except OSError:
        pass
    return fpath


def is_legacy_grace_active(deploy_ts: Optional[float] = None) -> bool:
    """旧 PRE_SECRET 30 天 grace 期内? deploy_ts 默认是 17b 落地 ts.
    HC-G4 平滑过渡: 旧 4 remote-node ws 连接不破.
    """
    # 17b 落地 ts: 2026-05-01T09:36Z (approx, dispatcher chat)
    # unix: 1777625760 ≈ 2026-05-01 09:36 UTC
    if deploy_ts is None:
        deploy_ts = float(os.environ.get("PRE_LEGACY_DEPLOY_TS", "1777625760"))
    return time.time() < deploy_ts + _LEGACY_GRACE_SEC


def validate_5cond_and(strict: bool = False) -> tuple[bool, list[str]]:
    """Phase D NS-M13: 5 条件 AND 启动校验 fail-closed.
    返 (ok, list[str of failed cond names]).
    strict=True 时 fail 调用方应 raise SystemExit (master/daemon 启动 fail-fast).
    """
    import stat
    failed = []
    if not _SECRETS_DIR.exists():
        # cond5 secret dir 不存在 (无 per-node secret 文件) — Phase A v2 17b 已建, 现 Phase D 强制门
        failed.append("cond5_secret_dir_missing")
    else:
        # cond2 dir chmod 700
        d_mode = stat.S_IMODE(_SECRETS_DIR.stat().st_mode)
        if d_mode != 0o700:
            failed.append(f"cond2_dir_chmod_{oct(d_mode)}_expected_700")
        # cond1 各 .json 文件 chmod 600
        for f in _SECRETS_DIR.glob("*.json"):
            f_mode = stat.S_IMODE(f.stat().st_mode)
            if f_mode != 0o600:
                failed.append(f"cond1_file_{f.name}_chmod_{oct(f_mode)}_expected_600")
        # cond5 各 secret 文件存在 (至少一个 .json 文件)
        json_files = list(_SECRETS_DIR.glob("*.json"))
        if not json_files:
            failed.append("cond5_no_json_files_in_secrets_dir")
    # cond4 owner non-root
    if os.geteuid() == 0:
        failed.append("cond4_running_as_root")
    # cond3 umask 077 — process-level set, 启动 main() 调 os.umask(0o077)
    # 这里只能软检 (umask 是 process state, 不能从 file 读)
    # Phase D 实施时主调用方需 os.umask(0o077) at process start
    return (len(failed) == 0, failed)


def hmac_for_manifest(node_id: str, file_path: str, sha256: str, ts: float) -> str:
    """Phase D + Phase A ( +):
    生成 sync_manifest 表 row_hmac, 防 master.db 篡改后伪造 manifest.

    用 secret_hash (不 raw secret, master 不持 raw) 作 HMAC key.
    远端 daemon 拿 raw secret env 计算 sha256 hash, 然后用 hash 验 manifest.

    Returns: HMAC sha256 hex string, or empty string if node secret missing.
    """
    import hmac as _hmac
    entry = NODE_SECRETS.get(node_id)
    if not entry:
        return ""
    secret_hash = entry.get("secret_hash", "")
    if not secret_hash:
        return ""
    msg = f"{file_path}|{sha256}|{ts:.6f}".encode("utf-8")
    return _hmac.new(secret_hash.encode("utf-8"), msg, hashlib.sha256).hexdigest()


# 启动时立即加载
load_node_secrets()
