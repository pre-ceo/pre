"""
pre 报告模块
关键发现 → report 文件 + git tag + 通知

report 放在项目的 {cwd}/pre/reports/ 目录
git tag 打在项目仓库 (不是 pre 仓库)
"""
import os
import json
import subprocess
from datetime import datetime, timezone

from .notify import send_notification


def report_finding(cwd: str, level: str, title: str, content: str,
                   session_id: str = "", tag_prefix: str = "finding") -> dict:
    """
    记录关键发现

    Args:
        cwd: 项目工作目录
        level: INFO / WARNING / CRITICAL
        title: 发现标题
        content: 发现内容 (markdown)
        session_id: 会话 ID
        tag_prefix: git tag 前缀

    Returns:
        {"report_path": ..., "tag": ..., "notified": bool}
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    project_name = os.path.basename(cwd) if cwd else "unknown"

    result = {"report_path": "", "tag": "", "notified": False}

    # --- 1. 写 report 文件 ---
    report_dir = os.path.join(cwd, "pre", "reports")
    os.makedirs(report_dir, exist_ok=True)

    filename = f"{ts}-{level.lower()}-{_slugify(title)}.md"
    report_path = os.path.join(report_dir, filename)

    report_content = (
        f"# [{level}] {title}\n\n"
        f"- **Time**: {now.isoformat()}\n"
        f"- **Project**: {project_name}\n"
        f"- **Session**: {session_id[:12]}\n"
        f"- **Level**: {level}\n\n"
        f"## Content\n\n"
        f"{content}\n"
    )

    try:
        with open(report_path, "w") as f:
            f.write(report_content)
        result["report_path"] = report_path
    except OSError:
        pass

    # --- 2. git tag ---
    tag_name = f"{tag_prefix}/{level.lower()}/{ts}"
    tag_message = f"[{level}] {title}"
    try:
        subprocess.run(
            ["git", "tag", "-a", tag_name, "-m", tag_message],
            cwd=cwd,
            capture_output=True,
            timeout=10,
        )
        result["tag"] = tag_name
    except (subprocess.TimeoutExpired, OSError):
        pass

    # --- 3. 通知 ---
    notified = send_notification(
        title=f"[{level}] {title}",
        body=content[:300],
        level=level,
        project=project_name,
    )
    result["notified"] = notified

    return result


def _slugify(text: str) -> str:
    """简单的 slug 化: 取前 30 字符, 替换非字母数字为 -"""
    slug = ""
    for c in text[:30].lower():
        if c.isalnum() or c == "-":
            slug += c
        elif c in (" ", "_", "/"):
            slug += "-"
    return slug.strip("-") or "untitled"
