"""cron_install.py — schedules.json merge 单测.

覆盖:
  make_entry              schema (id/enabled/type/every_seconds/target_node/cmd)
  merge_schedule          空文件 / 与现有 entries 共存 / 同 id 更新 / 坏 json / 缺 key
  install_schedule        原子写, 不留 tmp
  install_schedule        幂等 (第二次覆盖 cmd)
  cron_trigger.sh         脚本存在, shebang + 关键内容
"""
from __future__ import annotations

import json
from pathlib import Path

from gover_review.cron_install import (
    EVERY_SECONDS,
    SCHEDULE_ID,
    install_schedule,
    make_entry,
    merge_schedule,
)


def test_make_entry_shape():
    e = make_entry("/abs/trigger.sh")
    assert e["id"] == SCHEDULE_ID
    assert e["enabled"] is True
    assert e["type"] == "interval"
    assert e["every_seconds"] == EVERY_SECONDS
    assert e["target_node"] == "local"
    assert e["cmd"] == ["bash", "/abs/trigger.sh"]


def test_make_entry_accepts_path_object(tmp_path):
    e = make_entry(tmp_path / "t.sh")
    assert e["cmd"][1] == str(tmp_path / "t.sh")


def test_merge_into_missing_file(tmp_path):
    doc = merge_schedule(tmp_path / "absent.json", make_entry("/p/x.sh"))
    assert doc["version"] == 1
    assert len(doc["schedules"]) == 1
    assert doc["schedules"][0]["id"] == SCHEDULE_ID


def test_merge_keeps_other_entries(tmp_path):
    p = tmp_path / "schedules.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "schedules": [
                    {
                        "id": "other-1",
                        "type": "daily",
                        "time": "08:00",
                        "cmd": ["x"],
                    }
                ],
            }
        )
    )
    doc = merge_schedule(p, make_entry("/p/x.sh"))
    ids = [s["id"] for s in doc["schedules"]]
    assert "other-1" in ids
    assert SCHEDULE_ID in ids
    assert len(doc["schedules"]) == 2


def test_merge_idempotent_updates_in_place(tmp_path):
    p = tmp_path / "schedules.json"
    p.write_text(json.dumps({"version": 1, "schedules": [make_entry("/old.sh")]}))
    doc = merge_schedule(p, make_entry("/new.sh"))
    matching = [s for s in doc["schedules"] if s["id"] == SCHEDULE_ID]
    assert len(matching) == 1
    assert matching[0]["cmd"] == ["bash", "/new.sh"]


def test_merge_bad_json_treated_as_empty(tmp_path):
    p = tmp_path / "schedules.json"
    p.write_text("not json")
    doc = merge_schedule(p, make_entry("/p/x.sh"))
    assert len(doc["schedules"]) == 1


def test_merge_doc_not_a_dict(tmp_path):
    p = tmp_path / "schedules.json"
    p.write_text('["nope"]')
    doc = merge_schedule(p, make_entry("/p/x.sh"))
    assert doc["schedules"][0]["id"] == SCHEDULE_ID


def test_merge_missing_schedules_key(tmp_path):
    p = tmp_path / "schedules.json"
    p.write_text('{"version": 2}')
    doc = merge_schedule(p, make_entry("/p/x.sh"))
    assert doc["schedules"][0]["id"] == SCHEDULE_ID
    assert doc["version"] == 2  # 不覆盖现有 version


def test_merge_schedules_not_a_list(tmp_path):
    p = tmp_path / "schedules.json"
    p.write_text('{"version": 1, "schedules": "broken"}')
    doc = merge_schedule(p, make_entry("/p/x.sh"))
    assert isinstance(doc["schedules"], list)
    assert doc["schedules"][0]["id"] == SCHEDULE_ID


def test_install_schedule_creates_parent(tmp_path):
    p = tmp_path / "cron" / "schedules.json"
    install_schedule("/p/x.sh", p)
    assert p.exists()
    doc = json.loads(p.read_text())
    assert doc["schedules"][0]["id"] == SCHEDULE_ID


def test_install_schedule_atomic_no_tmp_left(tmp_path):
    p = tmp_path / "schedules.json"
    install_schedule("/p/x.sh", p)
    install_schedule("/p/y.sh", p)
    leftover = [x for x in tmp_path.iterdir() if x.name.startswith(".schedules.")]
    assert leftover == []


def test_install_schedule_idempotent_updates_cmd(tmp_path):
    p = tmp_path / "schedules.json"
    install_schedule("/p/x.sh", p)
    install_schedule("/p/y.sh", p)
    doc = json.loads(p.read_text())
    matching = [s for s in doc["schedules"] if s["id"] == SCHEDULE_ID]
    assert len(matching) == 1
    assert matching[0]["cmd"] == ["bash", "/p/y.sh"]


# ---------- cron_trigger.sh real file sanity ----------

def _trigger_script_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "gover_review"
        / "cron_trigger.sh"
    )


def test_cron_trigger_script_exists():
    p = _trigger_script_path()
    assert p.exists()
    assert p.read_text().startswith("#!")


def test_cron_trigger_script_executable():
    import os

    p = _trigger_script_path()
    mode = p.stat().st_mode
    assert mode & 0o111  # any exec bit set


def test_cron_trigger_script_handles_existing_session():
    txt = _trigger_script_path().read_text()
    assert "tmux has-session" in txt
    assert "skip" in txt.lower()


def test_cron_trigger_script_invokes_spawn_agent():
    txt = _trigger_script_path().read_text()
    assert "spawn_agent.sh" in txt
    assert "gover_review" in txt
