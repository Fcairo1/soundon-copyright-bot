from copyright_alert import claim_events as ce

HEADER = ["UPC", "Status", "Date Received", "Lark Message ID", "Admin Action Taken", "ref_code"]


def _values(*rows):
    return [HEADER, *rows]


def _index():
    return {name: i for i, name in enumerate(HEADER)}


def test_ms_to_utc_str():
    assert ce.ms_to_utc_str("1758370921000") == "2025-09-20 12:22:01"


def test_status_helpers_ignore_emoji():
    assert ce.is_am_terminal("🔴 Confirm Takedown")
    assert ce.is_am_terminal("✅ Resolved")
    assert not ce.is_am_terminal("🔍 Investigating")
    assert not ce.is_am_terminal("")


def test_record_status_event_builds_row(monkeypatch):
    sent = []
    monkeypatch.setattr(ce, "append_event", lambda *a, **k: sent.append((a, k)) or True)
    values = _values(["123", "🔍 Investigating", "2026-09-01", "om_abc", "", "ref:1"])
    assert ce.record_status_event(values, _index(), 2, new_status="🔴 Confirm Takedown", region="BR")
    (args, kwargs) = sent[0]
    assert args == ("om_abc", "am_action", "🔴 Confirm Takedown", "card", "BR")
    assert kwargs == {"prev_status": "🔍 Investigating", "ref_code": "ref:1", "upc": "123"}


def test_record_status_event_first_action_has_blank_prev(monkeypatch):
    sent = []
    monkeypatch.setattr(ce, "append_event", lambda *a, **k: sent.append(k) or True)
    values = _values(["123", "", "2026-09-01", "om_abc", "", ""])
    ce.record_status_event(values, _index(), 2, new_status="🔍 Investigating", region="BR")
    assert sent[0]["prev_status"] == ""


def test_record_status_event_skips_noop_reclick(monkeypatch):
    sent = []
    monkeypatch.setattr(ce, "append_event", lambda *a, **k: sent.append(1) or True)
    values = _values(["123", "🔴 Confirm Takedown", "2026-09-01", "om_abc", "", ""])
    assert not ce.record_status_event(values, _index(), 2, new_status="Confirm Takedown", region="BR")
    assert sent == []


def test_record_status_event_uses_key_hint_when_row_has_no_message_id(monkeypatch):
    sent = []
    monkeypatch.setattr(ce, "append_event", lambda *a, **k: sent.append(a) or True)
    values = _values(["123", "", "2026-09-01", "", "", ""])
    ce.record_status_event(values, _index(), 2, new_status="✅ Resolved", region="US", key_hint="om_hint")
    assert sent[0][0] == "om_hint"


def test_append_event_queues_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ce, "PENDING_FILE", tmp_path / "pending.jsonl")

    def boom(rows):
        raise RuntimeError("network")

    monkeypatch.setattr(ce, "_append_rows", boom)
    assert ce.append_event("om_abc", "am_action", "x", "card", "BR") is False
    assert (tmp_path / "pending.jsonl").read_text().count("\n") == 1

    monkeypatch.setattr(ce, "_append_rows", lambda rows: None)
    assert ce.flush_pending() == 1
    assert not (tmp_path / "pending.jsonl").exists()


def test_append_event_requires_key():
    assert ce.append_event("", "am_action", "x", "card", "BR") is False


def _ev(key, etype, status=""):
    return {"claim_key": key, "event_type": etype, "status": status}


def test_is_message_id():
    assert ce.is_message_id("om_x100b67d9e13720ace2f93dd6c76c7cb")
    assert not ce.is_message_id("2026-09-15 15:41")
    assert not ce.is_message_id("")
    assert not ce.is_message_id("om_")


def test_record_status_event_ignores_invalid_row_key_and_uses_hint(monkeypatch):
    sent = []
    monkeypatch.setattr(ce, "append_event", lambda *a, **k: sent.append(a) or True)
    values = _values(["123", "", "2026-09-01", "2026-09-15 15:41", "", ""])
    ce.record_status_event(values, _index(), 2, new_status="🔍 Investigating", region="BR", key_hint="om_goodhint123")
    assert sent[0][0] == "om_goodhint123"


def test_record_status_event_skips_when_no_valid_key(monkeypatch):
    sent = []
    monkeypatch.setattr(ce, "append_event", lambda *a, **k: sent.append(a) or True)
    values = _values(["123", "", "2026-09-01", "2026-09-15 15:41", "", ""])
    assert not ce.record_status_event(values, _index(), 2, new_status="🔍 Investigating", region="BR", key_hint="")
    assert sent == []


def test_plan_card_posted_skips_invalid_and_already_unavailable():
    values = _values(
        ["1", "", "d", "om_validone1", "", ""],
        ["2", "", "d", "2026-09-15 15:41", "", ""],     # invalid id
        ["3", "", "d", "om_gone12345", "", ""],          # already marked unavailable
    )
    todo = ce.plan_card_posted(values, [_ev("om_gone12345", "card_unavailable")], "BR")
    assert [t["claim_key"] for t in todo] == ["om_validone1"]
    assert ce.find_invalid_message_id_rows(values) == [3]


def test_run_daily_sync_marks_unavailable_and_keeps_going(monkeypatch):
    from copyright_alert import handle_callback

    values = _values(["1", "", "d", "om_gone12345", "", ""], ["2", "", "d", "om_okay12345", "", ""],
                     ["3", "", "d", "om_flaky12345", "", ""])
    monkeypatch.setattr(handle_callback, "read_sheet_values", lambda region=None: values)
    monkeypatch.setattr(ce, "read_events", lambda: [])
    monkeypatch.setattr(ce, "flush_pending", lambda: 0)
    monkeypatch.setattr(ce.time, "sleep", lambda s: None)
    wrote = []
    monkeypatch.setattr(ce, "_append_rows", lambda rows: wrote.append(rows))

    def fake(key):
        if key == "om_gone12345":
            raise ce.MessageUnavailable("HTTP 400")
        if key == "om_flaky12345":
            return None          # transient: no event, retried next run
        return "2026-09-01 12:00:00"

    summary = ce.run_daily_sync("BR", create_time_fn=fake)
    types = sorted((r[0], r[2]) for r in wrote[0])
    assert types == [("om_gone12345", "card_unavailable"), ("om_okay12345", "card_posted")]
    assert summary["card_unavailable"] == 1 and summary["create_time_failed"] == 1


def test_plan_card_posted_only_missing_and_dedupes():
    values = _values(
        ["1", "", "2026-09-01", "om_a", "", ""],
        ["2", "", "2026-09-01", "om_b", "", ""],
        ["3", "", "2026-09-01", "om_b", "", ""],  # duplicate key
        ["4", "", "2026-09-01", "", "", ""],      # no key
    )
    todo = ce.plan_card_posted(values, [_ev("om_a", "card_posted")], "BR")
    assert [t["claim_key"] for t in todo] == ["om_b"]


def test_plan_admin_action_seen_rules():
    values = _values(
        ["1", "🔴 Confirm Takedown", "d", "om_a", "Yes", ""],                 # waiting + filled -> yes
        ["2", "🔴 Confirm Takedown", "d", "om_b", "No", ""],                  # plain No -> still open
        ["3", "🔴 Confirm Takedown", "d", "om_c", "", ""],                    # empty
        ["4", "🔴 Confirm Takedown", "d", "om_d", "Yes", ""],                 # no am_action event logged (pre-existing)
        ["5", "✅ Resolved", "d", "om_e", "Sent ✅ - agree ~ 2026-09-10", ""],  # waiting + filled -> yes
        ["6", "🔴 Confirm Takedown", "d", "om_f", "Yes", ""],                 # already seen
    )
    events = [
        _ev("om_a", "am_action", "🔴 Confirm Takedown"), _ev("om_b", "am_action", "🔴 Confirm Takedown"),
        _ev("om_c", "am_action", "🔴 Confirm Takedown"), _ev("om_e", "am_action", "✅ Resolved"),
        _ev("om_f", "am_action", "🔴 Confirm Takedown"), _ev("om_f", "admin_action_seen", "Yes"),
    ]
    out = ce.plan_admin_action_seen(values, events, "BR")
    assert sorted(o["claim_key"] for o in out) == ["om_a", "om_e"]


def test_run_daily_sync_dry_run_writes_nothing(monkeypatch):
    from copyright_alert import handle_callback

    values = _values(["1", "", "2026-09-01", "om_a", "", ""])
    monkeypatch.setattr(handle_callback, "read_sheet_values", lambda region=None: values)
    monkeypatch.setattr(ce, "read_events", lambda: [])
    monkeypatch.setattr(ce, "flush_pending", lambda: 0)
    monkeypatch.setattr(ce.time, "sleep", lambda s: None)
    wrote = []
    monkeypatch.setattr(ce, "_append_rows", lambda rows: wrote.append(rows))
    summary = ce.run_daily_sync("BR", dry_run=True, create_time_fn=lambda key: "2026-09-01 12:00:00")
    assert summary["rows_to_append"] == 1 and wrote == []


def test_run_daily_sync_appends_card_posted(monkeypatch):
    from copyright_alert import handle_callback

    values = _values(["1", "", "2026-09-01", "om_a", "", "ref:9"])
    monkeypatch.setattr(handle_callback, "read_sheet_values", lambda region=None: values)
    monkeypatch.setattr(ce, "read_events", lambda: [])
    monkeypatch.setattr(ce, "flush_pending", lambda: 0)
    monkeypatch.setattr(ce.time, "sleep", lambda s: None)
    wrote = []
    monkeypatch.setattr(ce, "_append_rows", lambda rows: wrote.append(rows))
    ce.run_daily_sync("BR", create_time_fn=lambda key: "2026-09-01 12:00:00")
    row = wrote[0][0]
    assert row[:5] == ["om_a", "2026-09-01 12:00:00", "card_posted", "", "backfill"]
    assert row[6] == "BR" and row[8] == "ref:9" and row[9] == "1"


class _FakeSheet:
    """Minimal stand-in for lark_auth.sheet_values_api / extract_sheet_values."""

    def __init__(self, rows, interfere_once=False):
        self.rows = {i + 1: list(r) for i, r in enumerate(rows)}   # row number -> cells
        self.puts = []
        self.interfere_once = interfere_once

    def api(self, method, url, sheet_id, cell_range, values=None, timeout=60):
        import re
        m = re.match(r"A(\d+):(?:[A-Z])(\d+)$", cell_range) or re.match(r"A(\d+):A(\d+)$", cell_range)
        start, end = int(m.group(1)), int(m.group(2))
        if method == "PUT":
            self.puts.append((start, end, values))
            for i, row in enumerate(values):
                self.rows[start + i] = list(row)
            if self.interfere_once:            # someone else overwrote our first row right after
                self.interfere_once = False
                self.rows[start] = ["someone_else"] + [""] * 9
            return {"code": 0}
        return {"rows": [self.rows.get(n, []) for n in range(start, end + 1)]}

    @staticmethod
    def extract(payload):
        return payload["rows"]


def _patch_sheet(monkeypatch, sheet):
    from copyright_alert import lark_auth

    monkeypatch.setattr(lark_auth, "sheet_values_api", sheet.api)
    monkeypatch.setattr(lark_auth, "extract_sheet_values", sheet.extract)
    monkeypatch.setattr(ce, "MAX_LOG_ROWS", 50)


def test_append_rows_writes_after_last_used_row(monkeypatch):
    sheet = _FakeSheet([["claim_key"], ["om_old1"], ["om_old2"]])
    _patch_sheet(monkeypatch, sheet)
    rows = [["om_a", "t", "card_posted", "", "backfill", "", "BR", "", "", ""],
            ["om_b", "t", "card_posted", "", "backfill", "", "BR", "", "", ""]]
    ce._append_rows(rows)
    assert sheet.puts == [(4, 5, rows)]


def test_append_rows_retries_when_rows_were_overwritten(monkeypatch):
    sheet = _FakeSheet([["claim_key"]], interfere_once=True)
    _patch_sheet(monkeypatch, sheet)
    ce._append_rows([["om_a", "t", "x", "", "s", "", "BR", "", "", ""]])
    assert [p[0] for p in sheet.puts] == [2, 3]      # second attempt lands on the next free row
    assert sheet.rows[3][0] == "om_a"


def test_append_rows_refuses_past_sheet_capacity(monkeypatch):
    import pytest

    sheet = _FakeSheet([["claim_key"]] + [[f"om_{i}"] for i in range(49)])   # rows 1..50 used
    _patch_sheet(monkeypatch, sheet)
    with pytest.raises(RuntimeError, match="full"):
        ce._append_rows([["om_new", "t", "x", "", "s", "", "BR", "", "", ""]])
