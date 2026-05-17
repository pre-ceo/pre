"""gover_review cron schedule 注入 pre_rule/cron/schedules.json (幂等 merge).

跑哪由 src/master/cron.py 决定: 30s tick 读 schedules.json, type=interval +
every_seconds=14400 → 4h 一次触发本 entry 的 cmd. cmd 用 asyncio.subprocess
(不走 shell), 必须传绝对路径.

U7 install.sh 调 install_schedule(<trigger.sh 绝对路径>, <schedules.json 路径>).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SCHEDULE_ID = "gover-review-4h"
EVERY_SECONDS = 14400  # 4h


def make_entry(trigger_script: Path | str) -> dict:
    """构造 schedules.json 用的 entry. cmd 必须绝对路径."""
    return {
        "id": SCHEDULE_ID,
        "enabled": True,
        "type": "interval",
        "every_seconds": EVERY_SECONDS,
        "target_node": "local",
        "cmd": ["bash", str(trigger_script)],
    }


def merge_schedule(schedules_file: Path | str, entry: dict) -> dict:
    """读现有 schedules.json (容错), idempotent merge, 返新 doc (不落盘)."""
    path = Path(schedules_file)
    if path.exists():
        try:
            with open(path) as f:
                doc = json.load(f)
        except (json.JSONDecodeError, OSError):
            doc = {"version": 1, "schedules": []}
    else:
        doc = {"version": 1, "schedules": []}

    if not isinstance(doc, dict):
        doc = {"version": 1, "schedules": []}
    doc.setdefault("version", 1)
    scheds = doc.get("schedules")
    if not isinstance(scheds, list):
        scheds = []
    doc["schedules"] = scheds

    sid = entry["id"]
    for i, s in enumerate(scheds):
        if isinstance(s, dict) and s.get("id") == sid:
            scheds[i] = entry
            return doc
    scheds.append(entry)
    return doc


def _atomic_write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".schedules.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_schedule(
    trigger_script: Path | str,
    schedules_file: Path | str,
) -> dict:
    """End-to-end: merge entry + 原子写盘."""
    entry = make_entry(trigger_script)
    doc = merge_schedule(schedules_file, entry)
    _atomic_write(Path(schedules_file), doc)
    return {"entry": entry, "schedules_file": str(schedules_file)}
