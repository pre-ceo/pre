"""
pre 通知模块
通过 webhook-notify API 发送推送通知
零外部依赖 (urllib 是 stdlib)
"""
import json
import os
from urllib.request import Request, urlopen
from urllib.error import URLError

from .config import RULE_ROOT


def load_notify_config() -> dict:
    """加载通知配置"""
    config_path = os.path.join(RULE_ROOT, "notify_config.json")
    default = {
        "enabled": False,
        "api_url": "",
        "device_id": "",
        "default_volume": 0.3,
        "critical_volume": 0.8,
        "critical_sound": "alarm.caf",
        "normal_sound": "wakeup.caf",
    }
    try:
        if os.path.isfile(config_path):
            with open(config_path) as f:
                default.update(json.load(f))
    except (json.JSONDecodeError, OSError):
        pass
    return default


def send_notification(title: str, body: str, level: str = "INFO",
                      project: str = "", tts_text: str = "") -> bool:
    """
    发送通知

    Args:
        title: 通知标题
        body: 通知正文
        level: INFO / WARNING / CRITICAL
        project: 项目名 (用于标识来源)
        tts_text: TTS 朗读文本 (默认用 body)

    Returns:
        True = 发送成功, False = 失败或未启用
    """
    cfg = load_notify_config()

    if not cfg.get("enabled") or not cfg.get("device_id") or not cfg.get("api_url"):
        return False

    api_url = cfg["api_url"].rstrip("/")
    is_critical = (level == "CRITICAL")

    # 构建前缀
    prefix = f"[{project}]" if project else "[pre]"
    full_title = f"{prefix} {title}"

    payload = {
        "title": full_title[:100],
        "body": body[:500],
        "tts_text": tts_text or body[:200],
        "critical": is_critical,
        "sound": cfg["critical_sound"] if is_critical else cfg["normal_sound"],
        "volume": cfg["critical_volume"] if is_critical else cfg["default_volume"],
    }

    # 支持 group 模式 (group_id) 或 device 模式 (device_id)
    group_id = cfg.get("group_id", "")
    device_id = cfg.get("device_id", "")

    if group_id:
        payload["group_id"] = group_id
        endpoint = f"{api_url}/api/v1/group/tts"
    elif device_id:
        payload["device_id"] = device_id
        endpoint = f"{api_url}/api/v1/device/tts"
    else:
        return False

    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(endpoint, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (URLError, OSError, Exception):
        return False
