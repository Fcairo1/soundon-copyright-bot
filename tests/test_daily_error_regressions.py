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


def test_send_dm_post_goes_through_the_bot_by_email_when_no_chat_or_open_id(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "RECIPIENT_CHAT_ID", "")
    monkeypatch.setattr(workflow, "RECIPIENT_OPEN_ID", "")
    monkeypatch.setattr(workflow, "RECIPIENT_EMAIL", "ops@example.com")
    monkeypatch.setattr(workflow, "FEISHU_IM_DIR", Path(tmp_path) / "missing-helper")
    sent = []
    monkeypatch.setattr(workflow, "_send_dm_post_via_bot",
                        lambda rid, title, lines, typ="open_id": sent.append((typ, rid, title)) or True)

    assert workflow.send_dm_post("Action alert", ["row 1"]) is True
    assert sent == [("email", "ops@example.com", "Action alert")]


def test_send_dm_post_prefers_chat_id_over_email(monkeypatch):
    monkeypatch.setattr(workflow, "RECIPIENT_CHAT_ID", "oc_1")
    monkeypatch.setattr(workflow, "RECIPIENT_OPEN_ID", "")
    sent = []
    monkeypatch.setattr(workflow, "_send_dm_post_via_bot",
                        lambda rid, title, lines, typ="open_id": sent.append((typ, rid)) or True)
    assert workflow.send_dm_post("t", ["b"]) is True
    assert sent == [("chat_id", "oc_1")]


def test_send_dm_post_reports_failure_when_bot_and_helper_both_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow, "RECIPIENT_CHAT_ID", "")
    monkeypatch.setattr(workflow, "RECIPIENT_OPEN_ID", "")
    monkeypatch.setattr(workflow, "FEISHU_IM_DIR", Path(tmp_path) / "missing-helper")
    monkeypatch.setattr(workflow, "_send_dm_post_via_bot", lambda *a, **k: False)
    assert workflow.send_dm_post("t", ["b"]) is False
