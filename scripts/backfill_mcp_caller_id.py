#!/usr/bin/env python3
"""backfill_mcp_caller_id.py — 给所有 PRE_AGENT_HOME/*/pre/agent_config.json 补 explicit mcp.caller_agent_id.

策略:
- 逐个 load config, 跑跟 pre_mcp/tools.py:_caller_from_agent_config 同样的解析逻辑
- 解析失败 (返 "") 或 mcp 字段不存在 → 推断 driver_type + 写 explicit mcp.caller_agent_id
- driver_type 推断:
    * 已有 cfg.driver_type / cfg.driver / cfg.cli=='codex' → 沿用
    * dir 名以 _codex 结尾 → cli-codex-local
    * 否则 → cli-claude-code-local
- project_name 推断: dir 名
- node_id: local (PRE_NODE_ID env override)
- dry-run 默认显示 plan; --apply 落盘
- 不改已有 mcp.caller_agent_id (仅补)
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

# Add src/ to path for common.paths import
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from common.paths import PRE_AGENT_HOME

NODE_ID = os.environ.get("PRE_NODE_ID", "local")
CURSOR_ROOT = Path(PRE_AGENT_HOME)


def resolve_caller(cfg: dict) -> str:
    """跟 pre_mcp/tools.py:_caller_from_agent_config 同逻辑 (含顶层 agent_id fallback)."""
    mcp_cfg = cfg.get("mcp") if isinstance(cfg.get("mcp"), dict) else {}
    explicit = mcp_cfg.get("caller_agent_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    driver_type = cfg.get("driver_type") or cfg.get("driver") or ""
    project_name = cfg.get("project_name") or ""
    if isinstance(driver_type, str) and driver_type and isinstance(project_name, str) and project_name:
        return f"{NODE_ID}.{driver_type}.{project_name}"
    top_aid = cfg.get("agent_id")
    if isinstance(top_aid, str) and top_aid.startswith(f"{NODE_ID}."):
        return top_aid
    return ""


def infer_driver_type(cfg: dict, project: str) -> str:
    if isinstance(cfg.get("driver_type"), str) and cfg["driver_type"]:
        return cfg["driver_type"]
    if isinstance(cfg.get("driver"), str) and cfg["driver"]:
        return cfg["driver"]
    if cfg.get("cli") == "codex":
        return "cli-codex-local"
    if project.endswith("_codex"):
        return "cli-codex-local"
    return "cli-claude-code-local"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually write changes")
    args = parser.parse_args()

    configs = sorted(CURSOR_ROOT.glob("*/pre/agent_config.json"))
    plans = []
    for cfg_path in configs:
        project = cfg_path.parent.parent.name
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[SKIP-PARSE] {project}: {e}", file=sys.stderr)
            continue
        existing = resolve_caller(cfg)
        if existing:
            mcp_explicit = (cfg.get("mcp") or {}).get("caller_agent_id")
            tag = "EXPLICIT" if mcp_explicit else "FALLBACK"
            print(f"[OK-{tag:8s}] {project:32s} → {existing}")
            continue
        driver = infer_driver_type(cfg, project)
        proposed = f"{NODE_ID}.{driver}.{project}"
        plans.append((cfg_path, project, cfg, proposed))
        print(f"[PATCH    ] {project:32s} → ADD mcp.caller_agent_id={proposed}")

    print(f"\nTotal: {len(configs)} configs, {len(plans)} need patch")

    if not plans:
        return 0

    if not args.apply:
        print("\n(dry-run; pass --apply to write)")
        return 0

    for cfg_path, project, cfg, proposed in plans:
        if not isinstance(cfg.get("mcp"), dict):
            cfg["mcp"] = {}
        cfg["mcp"]["caller_agent_id"] = proposed
        cfg["mcp"].setdefault("server", "pre")
        cfg_path.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"[WROTE    ] {cfg_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
