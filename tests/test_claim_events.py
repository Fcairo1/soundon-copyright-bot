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
