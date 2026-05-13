#!/usr/bin/env python3
"""清理 master DB 里 stale 的 usage_snapshot_v2 entry.

stale 定义: fetch_ts 早于 (now - max_age_sec). 默认 max_age_sec=3600 (1小时).
干跑模式 --dry-run 先列要删的; 不加才真删.

跑法:
  uv run python scripts/cleanup_stale_usage_snapshots.py --dry-run
  uv run python scripts/cleanup_stale_usage_snapshots.py

场景: 远端 push 了带坏字段 (used_pct=null reset_at=null) 的 snapshot, 修复后
不再 push 新坏数据, 但旧的没自动清. T-Deck 等消费方拉 snapshots list 看到这条
stale=true 的旧坏数据 → 用本工具清掉.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


DEFAULT_DB = Path.home() / ".pre" / "data" / "master.db"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DEFAULT_DB), help="master.db 路径")
    p.add_argument("--max-age-sec", type=int, default=3600,
                   help="超过此 age 视为 stale (默认 3600=1小时)")
    p.add_argument("--dry-run", action="store_true", help="只列不删")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        print(f"[cleanup] db not found: {args.db}", file=sys.stderr)
        return 1

    threshold_ts = time.time() - args.max_age_sec
    con = sqlite3.connect(args.db)
    cur = con.cursor()
    cur.execute(
        "SELECT provider, account, status, used_pct, reset_at, fetch_ts, "
        "collected_by_node FROM usage_snapshot_v2 WHERE fetch_ts < ? "
        "ORDER BY fetch_ts",
        (threshold_ts,),
    )
    rows = cur.fetchall()
    print(f"[cleanup] db={args.db}")
    print(f"[cleanup] threshold_ts={threshold_ts:.0f} (max_age={args.max_age_sec}s)")
    print(f"[cleanup] stale entries: {len(rows)}")
    for row in rows:
        prov, acct, status, used_pct, reset_at, fetch_ts, node = row
        age = int(time.time() - fetch_ts)
        print(f"  {prov} | {acct} | status={status} used_pct={used_pct} "
              f"reset_at={reset_at} | age={age}s ({age/3600:.1f}h) | node={node}")

    if not rows:
        print("[cleanup] nothing to do")
        con.close()
        return 0

    if args.dry_run:
        print("[cleanup] dry-run mode, not deleting")
        con.close()
        return 0

    cur.execute("DELETE FROM usage_snapshot_v2 WHERE fetch_ts < ?",
                (threshold_ts,))
    con.commit()
    print(f"[cleanup] deleted {cur.rowcount} rows")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
