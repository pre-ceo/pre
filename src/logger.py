"""
pre 日志模块
JSONL 格式, 按 UTC 日期分割文件
"""
import json
import os
from datetime import datetime, timezone


def log_event(log_dir: str, event: dict):
    """追加一条 JSONL 日志到 logs/pre_hook_YYYYMMDD.jsonl"""
    os.makedirs(log_dir, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = os.path.join(log_dir, f"pre_hook_{date_str}.jsonl")
    with open(log_file, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
