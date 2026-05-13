"""
pre usage prober — 一次抓取 3 家 LLM cli 配额状态.

通过 sys_claude / sys_gemini / sys_codex 三个 monitor agent 的 tmux session,
注入 /usage /status /model 等 cli 内置命令, capture pane parse.

零 LLM token 消耗 (cli 内置命令查 account quota API, 不调 LLM completion).

用法:
  from master.usage_prober import probe_all
  data = probe_all()  # 返回 {claude: {...}, gemini: {...}, codex: {...}, ts}
"""
from __future__ import annotations
import json
import re
import sys
import os
import time
import asyncio

# import path: pre/src/
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
from tmux_helper import send_to_tmux, send_key, capture_pane
from common.paths import PRE_RULE_ROOT, PRE_LOG_ROOT


# ---------- gemini /model parser ----------

def _parse_gemini_account(pane: str) -> str | None:
    """gemini account 从 ~/.gemini/google_accounts.json 读 active 字段.
    cli oauth-personal 模式下此文件含 {active: "<user>@example.com", old: [...]}, 跨 node
    若同账号 → 收敛到 1 行. pane 不含此信息 (banner 仅 "Signed in with Google /auth").
    """
    cfg = os.path.join(os.path.expanduser("~"), ".gemini", "google_accounts.json")
    if not os.path.isfile(cfg):
        return None
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        acct = data.get("active")
        if isinstance(acct, str) and "@" in acct:
            return acct.strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


def _parse_gemini(pane: str) -> dict:
    """gemini /model 输出: 三模型 (Flash / Flash Lite / Pro) 各自 % used + Resets.
    gemini cli 没 /quota 内置命令, 实际命令是 /model. /model 输出含 account-wide quota.
       - status='ok': 至少抓到一个模型用量 < 100%
       - status='limit_reached': 任一模型 100% used
       - status='probe_inconclusive': cli 卡 thinking / cancelled
       - status='status_bar_only': /model 没产出但状态栏可见
       - status='unknown': 啥都没抓到

    /model 输出样本 (在 ask 弹窗内):
       Flash       ▬▬...▬  8%   Resets: 7:08 AM (22h 44m)
       Flash Lite  ▬▬...▬  7%   Resets: 7:08 AM (22h 44m)
       Pro         ▬▬...▬  100% Resets: 6:20 PM (9h 57m)
    """
    out = {"status": "unknown", "raw_excerpt": pane[-1500:]}
    tail = pane[-1000:]

    # 1. cli 在 thinking / cancelled 状态 → probe_inconclusive
    if "Thinking..." in tail or "Request cancelled" in tail:
        out["status"] = "probe_inconclusive"

    # 2. /model 真实输出 — 各模型用量
    # 抓 Flash / Flash Lite / Pro 三行 (在 ask 弹窗内)
    models = {}
    for label, key in [("Flash Lite", "flash_lite"), ("Flash", "flash"), ("Pro", "pro")]:
        # "Flash Lite" 必须先匹配, 否则 "Flash" 会贪心抓到 "Flash Lite" 行
        pat = re.escape(label) + r"\s+[▬▮▭━▰▱]+\s+(\d+)%\s+Resets:\s+([\d:]+\s*[AP]M)\s*\(([^)]+)\)"
        m = re.search(pat, pane)
        if m:
            models[key] = {
                "percent_used": int(m.group(1)),
                "reset_at": m.group(2).strip(),
                "reset_in": m.group(3).strip(),
            }
    if models:
        out["models"] = models
        any_limit = any(v["percent_used"] >= 100 for v in models.values())
        out["status"] = "limit_reached" if any_limit else "ok"
        if any_limit:
            out["models_limited"] = [k for k, v in models.items() if v["percent_used"] >= 100]

    # 3. 旧 /quota 形态兼容 (历史遗留)
    m = re.search(r"Usage limit reached for (\S+?)\.", pane)
    if m:
        out["status"] = "limit_reached"
        out["model_limited"] = m.group(1)

    # 4. 底部状态栏 (cli 静态信息): active model + 该模型用量
    # 兼容 0.40.1 新形态 "Auto (Gemini 3) 4% used" (旧 0.40.0 是 "gemini-3-flash-preview 8% used").
    # /model 实际仍是弹窗 (含 3 model + Resets), 状态栏只有 cur 缺 reset; 状态栏数据照填,
    # 但不再升级 status — 让 status_bar_only/probe_inconclusive 触发上层 retry 真正抓 dialog.
    m = re.search(r"(Auto\s*\(Gemini[^)]+\)|gemini[\w.\-]+(?:-preview|-pro|-flash|-lite)?[\w.\-]*)\s+(\d+)% used", tail)
    if m:
        out["active_model"] = m.group(1).strip()
        out["active_model_percent_used"] = int(m.group(2))
        # 同步设 used_pct (顶层) 让 v2 表 used_pct 列可填
        if out.get("active_model_percent_used") is not None and "models" not in out:
            out.setdefault("session_percent_used", out["active_model_percent_used"])
        # 状态栏抓到但没 models dict → 标 status_bar_only, 让 retry 路径生效
        if "models" not in out and out["status"] in ("unknown", "ok"):
            out["status"] = "status_bar_only"
    # account 从 ~/.gemini/google_accounts.json disk 抓 (跨 node 收敛)
    acct = _parse_gemini_account(pane)
    if acct:
        out["account"] = acct
    return out


# ---------- codex /status parser ----------

def _parse_codex(pane: str) -> dict:
    out = {"status": "ok", "raw_excerpt": pane[-1500:]}
    m = re.search(r"5h limit:.*?(\d+)% left\s+\(resets\s+([\d:]+)\)", pane)
    if m:
        out["percent_left_5h"] = int(m.group(1))
        out["reset_5h"] = m.group(2)
    m = re.search(r"Weekly limit:.*?(\d+)% left.*?\(resets\s+([\d:]+ on \d+ \w+)\)", pane, re.DOTALL)
    if m:
        out["percent_left_week"] = int(m.group(1))
        out["reset_week"] = m.group(2)
    m = re.search(r"Account:\s+(\S+@\S+)\s+\(([^)]+)\)", pane)
    if m:
        out["account"] = m.group(1)
        out["plan"] = m.group(2)
    m = re.search(r"Model:\s+(\S+)\s*\([^)]*\)", pane)
    if m:
        out["model"] = m.group(1)
    if out.get("percent_left_5h") is not None and out["percent_left_5h"] < 5:
        out["status"] = "near_limit"
    if out.get("percent_left_5h") == 0:
        out["status"] = "limit_reached"
    return out


# ---------- claude /usage parser ----------

def _parse_claude(pane: str) -> dict:
    out = {"status": "ok", "raw_excerpt": pane[-1500:]}
    # 优先 /status 输出 'Email: xxx@xxx', fallback banner "xxx · API Usage Billing" (apikey),
    # 最后 fallback banner "xxx@xxx's Organization" (订阅版欢迎语).
    m = re.search(r"Email:\s+(\S+@\S+)", pane)
    if m:
        out["account"] = m.group(1).strip()
    else:
        m = re.search(r"^\s*([\w\-.]+)\s*[·•]\s*API Usage Billing", pane, re.MULTILINE)
        if m:
            out["account"] = m.group(1).strip()
            out["billing_mode"] = "apikey"
        else:
            m = re.search(r"(\S+@\S+)'s Organization", pane)
            if m:
                out["account"] = m.group(1).strip()
    # claude breakdown 比例 (没绝对 %)
    breakdown = {}
    for label_re, key in [
        (r"(\d+)% of your usage was at >\d+k context", "high_context_pct"),
        (r"(\d+)% of your usage came from sessions active for", "long_session_pct"),
        (r"(\d+)% of your usage was while \d+\+ sessions ran", "parallel_session_pct"),
    ]:
        m = re.search(label_re, pane)
        if m:
            breakdown[key] = int(m.group(1))
    if breakdown:
        out["breakdown"] = breakdown
    # Current session/week 绝对 %. reset 字段格式多样 ("9:20pm (Asia/Shanghai)" /
    # "May 7 at 10am (Asia/Shanghai)"), 抓到行尾 (避免 .* 贪心吃下文).
    m = re.search(r"Current session.*?(\d+)% used.*?Resets ([^\n]+?)(?=\s*\n)", pane, re.DOTALL)
    if m:
        out["session_percent_used"] = int(m.group(1))
        out["session_reset"] = m.group(2).strip()
    m = re.search(r"Current week.*?(\d+)% used.*?Resets ([^\n]+?)(?=\s*\n)", pane, re.DOTALL)
    if m:
        out["week_percent_used"] = int(m.group(1))
        out["week_reset"] = m.group(2).strip()
    m = re.search(r"Total cost:\s+\$(\d+\.\d+)", pane)
    if m:
        out["session_cost_usd"] = float(m.group(1))
    if "Extra usage not enabled" in pane:
        out["extra_usage_enabled"] = False
    elif "Extra usage" in pane:
        out["extra_usage_enabled"] = True
    if out.get("week_percent_used", 0) >= 90:
        out["status"] = "near_limit"
    return out


# ---------- 抓取流程 ----------

async def _probe_one(session: str, command: str, parser,
                     timeout_total: float = 6.0,
                     cleanup_key: str | None = None,
                     extra_keywords: tuple = ()):
    """注入 cli 命令 + 等输出 + parse + 可选 cleanup. 失败返 {error: ...}.
    command="" → 跳过 send, 直接 capture (cli 状态栏 quota 即权威源时用).
    cleanup_key: 抓完后发该按键 (e.g. 'Escape' 关 ask 弹窗); None 不发.
    extra_keywords: 显式传入 = 严格模式, 必须 **全部** 出现才认为输出就绪
       (e.g. gemini /model 必须同时见 Resets: + Flash + Pro 才算 dialog 渲染完);
       未传 = 任一默认 kws (limit/resets/Account/...) 命中即 break.
    """
    try:
        if command:
            await asyncio.to_thread(send_to_tmux, session, command)
        deadline = time.time() + timeout_total
        last_pane = ""
        default_kws = ("limit", "resets", "Account", "Auth", "Last 24h", "% used")
        strict_mode = bool(extra_keywords)
        while time.time() < deadline:
            await asyncio.sleep(0.4)
            pane = await asyncio.to_thread(capture_pane, session, 80)
            if pane and pane != last_pane:
                last_pane = pane
                if strict_mode:
                    if all(kw in pane for kw in extra_keywords):
                        break
                else:
                    if any(kw in pane for kw in default_kws):
                        break
        if not last_pane:
            last_pane = await asyncio.to_thread(capture_pane, session, 80)
        result = parser(last_pane or "")
        if cleanup_key:
            try:
                await asyncio.to_thread(send_key, session, cleanup_key)
            except Exception:
                pass
        return result
    except Exception as e:
        return {"error": str(e)[:200]}


# provider → spec SoT.
# 严白名单: session 名硬编码, 杜绝业务 agent 被误抓.
# default_enabled 字段: claude_foxbn 等"单独模型"默认 disabled
# (后续提供 POST /api/v1/usage/external 统一 API 输入, 不走 cli pane probe).
# 实际 enabled 由 _enabled_providers() 读 pre_rule/usage_probe.json 决定.
_PROBE_SPECS: dict[str, dict] = {
    "claude": {
        "session": "sys_claude",
        "command": "/usage",
        # claude v2.1 /usage 是 modal dialog (末尾 "Esc to cancel"),
        # 抓完必发 Escape 关弹窗, 否则 attached 用户看到 cli 被 /usage 占着
        "parser_kwargs": {"cleanup_key": "Escape"},
        "default_enabled": True,
        # 不加 --dangerously-skip-permissions (会引入二次"Bypass Permissions"确认弹窗,
        # 默认指针指 "No, exit"). sys_claude 只跑 /usage / /status slash, 不触发 file
        # edit ask UI, 普通启动 + trust dialog auto-confirm 即可工作.
        "spawn_cli": "claude",
    },
    "claude_foxbn": {
        "session": "sys_claude_foxbn",
        "command": "/usage",
        "parser_kwargs": {"cleanup_key": "Escape"},
        # 默认不抓 — apikey 模式 cli pane 数据不全, 后续走 POST /api/v1/usage/external 输入
        "default_enabled": False,
        "spawn_cli": "claude",
    },
    "gemini": {
        "session": "sys_gemini",
        # gemini cli /model 是 "Select Model" dialog 含底部 "Model usage" panel:
        # Flash X% / Flash Lite Y% / Pro Z% + Resets. autoExecute=true 触发 dialog,
        # 不烧 LLM token. 抓完必发 Escape 关弹窗. 配合 strict_mode 等全部 keyword.
        "command": "/model",
        "parser_kwargs": {
            "cleanup_key": "Escape",
            "extra_keywords": ("Resets:", "Flash", "Pro"),
            "timeout_total": 10.0,
        },
        "default_enabled": True,
        "spawn_cli": "gemini",
    },
    "codex": {
        "session": "sys_codex",
        "command": "/status",
        "parser_kwargs": {},
        "default_enabled": True,
        "spawn_cli": "codex",
    },
}


def _load_provider_config() -> dict:
    """从 pre_rule/usage_probe.json 读 provider enable/disable 配置.
    schema: {"providers": {"claude": {"enabled": true}, "claude_foxbn": {"enabled": false}, ...}}
    缺文件 / 缺 key 用 _PROBE_SPECS[p]["default_enabled"].
    """
    cfg_path = os.path.join(PRE_RULE_ROOT, "usage_probe.json")
    if not os.path.isfile(cfg_path):
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def is_provider_enabled(provider: str) -> bool:
    """provider 是否启用 cli pane probe. 配置 > default_enabled."""
    spec = _PROBE_SPECS.get(provider)
    if not spec:
        return False
    cfg = _load_provider_config()
    p_cfg = (cfg.get("providers") or {}).get(provider) or {}
    if "enabled" in p_cfg:
        return bool(p_cfg["enabled"])
    return bool(spec.get("default_enabled", True))


async def _probe_all_async(providers: list[str] | None = None) -> dict:
    """并发跑指定 providers (默认全 3 家). 缺失 tmux session → 跳过, 不抛错.

    providers 参数让远端 (e.g. 没装 gemini/codex cli 的 node) 可指定只抓 claude.
    session 不存在直接 skipped 不浪费 6s timeout.
    """
    if providers is None:
        providers = list(_PROBE_SPECS.keys())
    # 严白校验 — 防 caller 传非法 provider
    invalid = [p for p in providers if p not in _PROBE_SPECS]
    if invalid:
        raise ValueError(f"invalid providers: {invalid} (allowed: {list(_PROBE_SPECS)})")

    # 配置过滤 — 关掉的 provider (e.g. claude_foxbn 默认 disabled) 直接 skip
    enabled_providers = [p for p in providers if is_provider_enabled(p)]
    config_skipped = {p: "provider disabled in config / default" for p in providers
                       if p not in enabled_providers}

    # session 存在性检查 (sync, ms 级 syscall, 0 LLM cost) — 只抓存在的
    from tmux_helper import has_session  # noqa: WPS433
    active_providers: list[str] = []
    skipped: dict[str, str] = dict(config_skipped)
    for p in enabled_providers:
        sess = _PROBE_SPECS[p]["session"]
        if await asyncio.to_thread(has_session, sess):
            active_providers.append(p)
        else:
            skipped[p] = f"tmux session {sess!r} not found"

    # 先 send Escape 关闭可能在屏的弹出框
    for p in active_providers:
        try:
            await asyncio.to_thread(send_key, _PROBE_SPECS[p]["session"], "Escape")
        except Exception:
            pass
    await asyncio.sleep(0.3)

    parser_map = {
        "claude": _parse_claude,
        "claude_foxbn": _parse_claude,  # apikey claude 复用 _parse_claude (cost 字段格式相同)
        "gemini": _parse_gemini,
        "codex": _parse_codex,
    }

    async def _probe_one_provider(p: str):
        spec = _PROBE_SPECS[p]
        # claude /usage 不返 account, 必须先 /status 抓 Email 再 /usage 抓 quota
        if p in ("claude", "claude_foxbn"):
            r_status = await _probe_one(spec["session"], "/status", parser_map[p],
                                         timeout_total=4.0)
            await asyncio.sleep(0.3)
            try:
                await asyncio.to_thread(send_key, spec["session"], "Escape")
            except Exception:
                pass
            await asyncio.sleep(0.2)
            r_usage = await _probe_one(spec["session"], spec["command"], parser_map[p],
                                        **spec["parser_kwargs"])
            # 合并: usage 是主 (含 quota), status 仅补 account/billing_mode
            if isinstance(r_usage, dict) and isinstance(r_status, dict):
                for k in ("account", "billing_mode"):
                    if k in r_status and k not in r_usage:
                        r_usage[k] = r_status[k]
                return r_usage
            return r_usage if isinstance(r_usage, dict) else r_status
        else:
            return await _probe_one(spec["session"], spec["command"], parser_map[p],
                                     **spec["parser_kwargs"])

    raw_results = await asyncio.gather(
        *[_probe_one_provider(p) for p in active_providers],
        return_exceptions=True,
    )
    results: dict[str, dict] = {}
    for p, r in zip(active_providers, raw_results):
        results[p] = r if not isinstance(r, Exception) else {"error": str(r)}

    # gemini single retry on incomplete (only if gemini was probed)
    if "gemini" in active_providers:
        gemini_r = results.get("gemini") or {}
        if isinstance(gemini_r, dict) and gemini_r.get("status") in (
                "probe_inconclusive", "status_bar_only", "unknown"):
            try:
                await asyncio.to_thread(send_key, "sys_gemini", "Escape")
                await asyncio.sleep(0.5)
                await asyncio.to_thread(send_key, "sys_gemini", "Escape")
                await asyncio.sleep(1.0)
            except Exception:
                pass
            retry_r = await _probe_one("sys_gemini", "/model", _parse_gemini,
                                       cleanup_key="Escape",
                                       extra_keywords=("Resets:", "Flash", "Pro"),
                                       timeout_total=8.0)
            if isinstance(retry_r, dict) and retry_r.get("status") in ("limit_reached", "ok"):
                results["gemini"] = retry_r

    # 跳过的 provider 标记 (上层可见)
    for p, reason in skipped.items():
        results[p] = {"status": "skipped", "skipped_reason": reason}

    results["probed_ts"] = time.time()
    return results


def probe_all() -> dict:
    """同步入口 (master 后台 task 用 to_thread 调用).

    解析后调 _persist_valid_snapshots 把合法结果 upsert usage_snapshot 表;
    不合法 → 写 usage_errors/{provider}_{ts}.txt 不动 DB.
    """
    data = asyncio.run(_probe_all_async())
    try:
        _persist_valid_snapshots(data)
    except Exception:  # noqa: BLE001 — fail-safe
        pass
    return data


_VALID_STATUS = {"ok", "limit_reached"}


def _validate_parsed(parsed: dict) -> tuple[bool, str]:
    """validation. 返 (ok, reason). valid status: ok | limit_reached.
    其他 (probe_inconclusive/unknown/status_bar_only/error/missing) 拒.
    """
    if not isinstance(parsed, dict):
        return False, "not_dict"
    if parsed.get("error"):
        return False, f"probe_error: {str(parsed['error'])[:100]}"
    s = parsed.get("status")
    if s not in _VALID_STATUS:
        return False, f"invalid_status:{s!r}"
    return True, ""


def _extract_used_pct(parsed: dict) -> float | None:
    """从 parsed 抽主指标 used_pct (gemini 取 max, claude/codex 单值)."""
    models = parsed.get("models")
    if isinstance(models, dict) and models:
        pcts = []
        for v in models.values():
            if isinstance(v, dict):
                p = v.get("percent_used")
                if isinstance(p, (int, float)):
                    pcts.append(float(p))
        if pcts:
            return max(pcts)
    p = parsed.get("percent_used") or parsed.get("used_pct")
    if isinstance(p, (int, float)):
        return float(p)
    return None


def _extract_reset_at(parsed: dict) -> str | None:
    models = parsed.get("models")
    if isinstance(models, dict):
        for v in models.values():
            if isinstance(v, dict):
                r = v.get("reset_at") or v.get("reset_in")
                if r:
                    return str(r)[:64]
    r = parsed.get("reset_at") or parsed.get("resets")
    if r:
        return str(r)[:64]
    return None


def _persist_valid_snapshots(data: dict):
    """合法 status upsert usage_snapshot 表; 不合法写错误 pane 日志.
    严禁 status="unknown" 当合法存表 (HC-G11 vacuous truth 防护).
    """
    fetch_ts = float(data.get("probed_ts") or time.time())
    log_dir_env = os.environ.get(
        "PRE_LOG_DIR",
        PRE_LOG_ROOT)
    db_path = os.environ.get(
        "PRE_MASTER_DB",
        os.path.join(os.path.expanduser("~"), ".pre", "data", "master.db"))
    err_dir = os.path.join(log_dir_env, "usage_errors")
    try:
        from master.persistence import MasterDB
    except ImportError:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from persistence import MasterDB  # noqa: F401
        except ImportError:
            return
    try:
        db = MasterDB(db_path)
    except Exception:  # noqa: BLE001
        return
    for provider in ("claude", "gemini", "codex"):
        parsed = data.get(provider)
        if not isinstance(parsed, dict):
            _write_pane_error_log(err_dir, provider,
                                    {"reason": "missing_in_probe_result"},
                                    "", fetch_ts)
            continue
        ok, reason = _validate_parsed(parsed)
        raw_excerpt = parsed.get("raw_excerpt") or ""
        if not ok:
            _write_pane_error_log(err_dir, provider,
                                    {"reason": reason, "parsed_status": parsed.get("status"),
                                     "models_present": bool(parsed.get("models"))},
                                    raw_excerpt, fetch_ts)
            continue
        used_pct = _extract_used_pct(parsed)
        reset_at = _extract_reset_at(parsed)
        models = parsed.get("models") or {}
        ok_db = db.upsert_usage_snapshot(
            provider=provider, status=parsed["status"],
            models=models if isinstance(models, dict) else {},
            used_pct=used_pct, reset_at=reset_at,
            fetch_ts=fetch_ts, source="tmux_pane_parse",
            raw_excerpt=raw_excerpt,
        )
        if not ok_db:
            _write_pane_error_log(err_dir, provider,
                                    {"reason": "db_upsert_failed",
                                     "parsed_status": parsed.get("status")},
                                    raw_excerpt, fetch_ts)


def _write_pane_error_log(err_dir: str, provider: str,
                            meta: dict, raw_pane: str, fetch_ts: float):
    """错误 pane 日志: 不入 DB 但留排查痕迹.
    路径: <PRE_LOG_ROOT>/usage_errors/{provider}_{YYYYMMDD_HHMMSS}.txt chmod 600.
    """
    try:
        os.makedirs(err_dir, exist_ok=True)
        try:
            os.chmod(err_dir, 0o700)
        except OSError:
            pass
        from datetime import datetime, timezone
        ts_str = datetime.fromtimestamp(fetch_ts, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        fpath = os.path.join(err_dir, f"{provider}_{ts_str}.txt")
        body = (
            f"# usage_prober pane error log\n"
            f"# provider: {provider}\n"
            f"# fetch_ts: {fetch_ts}\n"
            f"# meta: {json.dumps(meta, ensure_ascii=False)}\n"
            f"# usage_prober validation 不通过, 不更新 DB. raw_excerpt 留排查.\n"
            f"---raw pane (≤2KB)---\n"
            f"{(raw_pane or '')[:2048]}\n"
        )
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(body)
        try:
            os.chmod(fpath, 0o600)
        except OSError:
            pass
        # rotation 30 天: 简单删超期文件
        cutoff = fetch_ts - 30 * 86400
        for fn in os.listdir(err_dir):
            fp = os.path.join(err_dir, fn)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
            except OSError:
                pass
    except (OSError, ValueError, ImportError):
        pass
