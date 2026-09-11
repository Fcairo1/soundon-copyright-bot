from pathlib import Path

from copyright_alert import daily_workflow as workflow
from copyright_alert import run_alert


def test_run_alert_col_letter_available_for_tracker_append_ranges():
    assert run_alert._col_letter(0) == "A"
    assert run_alert._col_letter(25) == "Z"
    assert run_alert._col_letter(26) == "AA"


def test_send_dm_post_skips_missing_feishu_helper(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "RECIPIENT_CHAT_ID", "")
    monkeypatch.setattr(workflow, "RECIPIENT_OPEN_ID", "")
    monkeypatch.setattr(workflow, "FEISHU_IM_DIR", Path(tmp_path) / "missing-helper")

    assert workflow.send_dm_post("title", ["body"]) is False
