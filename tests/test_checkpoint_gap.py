"""Checkpoint hold/advance decision and date persistence (the stuck US checkpoint)."""
import json
from datetime import datetime, timezone

import pytest

from copyright_alert import daily_workflow as wf

D = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
hold = wf.should_hold_checkpoint


def test_no_checkpoint_or_reached_never_holds():
    assert hold(None, False, D("2026-09-22"), None) is False
    assert hold("cp", True, D("2026-09-22"), None) is False


def test_missing_checkpoint_with_no_truncated_query_advances():
    assert hold("cp", False, None, None) is False


def test_truncated_legacy_checkpoint_without_date_holds():
    assert hold("cp", False, D("2026-09-22"), None) is True


def test_truncated_but_window_reaches_back_past_checkpoint_date_advances():
    # deleted checkpoint message dated Sep 16; truncated query still reaches Sep 10
    assert hold("cp", False, D("2026-09-10"), D("2026-09-16")) is False


def test_truncated_window_newer_than_checkpoint_date_holds():
    assert hold("cp", False, D("2026-09-22"), D("2026-09-16")) is True


def test_operator_flag_overrides_hold():
    assert hold("cp", False, D("2026-09-22"), None, accept_gap=True) is False


@pytest.fixture
def cp_file(tmp_path, monkeypatch):
    f = tmp_path / "cp.json"
    monkeypatch.setattr(wf, "CHECKPOINT_FILE", str(f))
    return f


def test_save_checkpoint_stores_date(cp_file):
    wf.save_checkpoint("m1", failed_message_ids=[], message_date="2026-10-08T16:07:21Z")
    assert json.loads(cp_file.read_text())["last_message_date"] == "2026-10-08T16:07:21Z"


def test_date_kept_while_id_unchanged_and_replaced_when_it_moves(cp_file):
    wf.save_checkpoint("m1", failed_message_ids=[], message_date="2026-10-08T16:07:21Z")
    wf.save_checkpoint("m1", failed_message_ids=[])                      # held run
    assert json.loads(cp_file.read_text())["last_message_date"] == "2026-10-08T16:07:21Z"
    wf.save_checkpoint("m2", failed_message_ids=[], message_date="2026-10-09T10:00:00Z")
    assert json.loads(cp_file.read_text())["last_message_date"] == "2026-10-09T10:00:00Z"
    wf.save_checkpoint("m3", failed_message_ids=[])                      # new ID, no date
    assert json.loads(cp_file.read_text())["last_message_date"] is None


def test_query_stats_flag_truncation_and_coverage():
    wf.LAST_FETCH_STATS.update({"queries": {}, "coverage_start": None})
    msgs = [{"date": f"2026-09-{d:02d}T10:00:00Z"} for d in range(1, 11)]
    wf._record_query_stats("big", msgs, cap=10)          # hit the cap
    wf._record_query_stats("small", msgs[:3], cap=10)    # not truncated
    assert wf.LAST_FETCH_STATS["queries"]["big"]["truncated"] is True
    assert wf.LAST_FETCH_STATS["queries"]["small"]["truncated"] is False
    assert wf.LAST_FETCH_STATS["coverage_start"] == D("2026-09-01T10:00:00")
