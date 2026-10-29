from copyright_alert import bot_runtime as br
from copyright_alert import daily_workflow as dw
from copyright_alert import run_alert as ra


def test_retraction_detection_requires_exact_sender_and_phrase():
    meta = {
        "head_from": {"mail_address": "infringement-claim-response@spotify.com"},
        "date_formatted": "2026-09-10T12:30:00Z",
    }
    body = "Hello,\n\nThe claimant has retracted the claim.\n\nUPC: 123456789012"
    assert ra.is_retraction_email(body, meta=meta) is True

    wrong_sender = {
        "head_from": {"mail_address": "rights@fuga.com"},
        "date_formatted": "2026-09-10T12:30:00Z",
    }
    assert ra.is_retraction_email(body, meta=wrong_sender) is False

    instructional_body = "Please work towards the notice being retracted as soon as possible."
    assert ra.is_retraction_email(instructional_body, meta=meta) is False


def test_extract_retraction_fields_uses_claim_labels():
    meta = {
        "head_from": {"mail_address": "infringement-claim-response@spotify.com"},
        "date_formatted": "2026-09-10T12:30:00Z",
    }
    body = """
Hello,

The claimant has retracted the claim.
Content Title: Midnight Echo
Artist: Example Artist
UPC: 123456789012
Spotify URI: spotify:track:abc123
ref:_00DA1ABC._5005xYz123:ref
"""
    fields = ra.extract_retraction_fields(body, meta=meta)

    assert fields["title"] == "Midnight Echo"
    assert fields["upc"] == "123456789012"
    assert fields["spotify_uri"] == "spotify:track:abc123"
    assert fields["ref_id"] == "ref:_00DA1ABC._5005xYz123:ref"
    assert fields["retracted_at"] == "2026-09-10T12:30:00Z"


def test_match_tracker_row_for_retraction_prefers_ref_code_and_open_row():
    values = [
        ["UPC", "Title", "Artist", "Status", "ref_code", "Retracted"],
        ["123456789012", "Older Match", "Artist A", "✅ Resolved", "ref:_old:ref", "Yes"],
        ["123456789012", "Current Match", "Artist A", "🔍 Investigating", "ref:_target:ref", ""],
        ["123456789012", "Same UPC Different Claim", "Artist A", "", "ref:_other:ref", ""],
    ]

    match = dw.match_tracker_row_for_retraction(
        values,
        {"upc": "123456789012", "ref_id": "ref:_target:ref"},
    )

    assert match is not None
    assert match["row_num"] == 3
    assert match["title"] == "Current Match"
    assert match["ref_match"] is True


def test_open_status_excludes_retracted_rows():
    row = {
        "Status": "🔍 Investigating",
        "Admin Action Taken": "",
        "Retracted": "Yes",
    }
    assert br._is_open_status(row) is False


def test_retraction_search_window_is_a_fixed_7_day_lookback():
    from datetime import datetime, timezone
    from copyright_alert import daily_workflow as dw

    now = datetime(2026, 10, 22, 21, 0, 0, tzinfo=timezone.utc)
    assert dw.retraction_window_start_iso(now) == "2026-10-15T21:00:00Z"
    assert dw._mail_search_timestamp(dw.retraction_window_start_iso(now)) is not None


def test_retraction_state_is_per_region_and_unmatched_stays_open(tmp_path, monkeypatch):
    from copyright_alert import run_alert as ra

    monkeypatch.setattr(ra, "RETRACTED_CLAIMS_FILE", str(tmp_path / "retracted.json"))
    # BR run sees a US retraction: unmatched -> must NOT block the US run.
    ra._save_retracted_claim("m1", {"region": "BR", "matched": False})
    assert ra.is_retraction_already_processed("m1", region="BR") is False
    assert ra.is_retraction_already_processed("m1", region="US") is False
    # US run applies it: processed for US only.
    ra._save_retracted_claim("m1", {"region": "US", "matched": True})
    assert ra.is_retraction_already_processed("m1", region="US") is True
    assert ra.is_retraction_already_processed("m1", region="BR") is False


def test_legacy_retraction_records_only_count_when_matched_for_same_region(tmp_path, monkeypatch):
    import json
    from copyright_alert import run_alert as ra

    f = tmp_path / "retracted.json"
    f.write_text(json.dumps({
        "old_matched_br": {"region": "BR", "matched": True},
        "old_unmatched_br": {"region": "BR", "matched": False},
    }))
    monkeypatch.setattr(ra, "RETRACTED_CLAIMS_FILE", str(f))
    assert ra.is_retraction_already_processed("old_matched_br", region="BR") is True
    assert ra.is_retraction_already_processed("old_matched_br", region="US") is False
    assert ra.is_retraction_already_processed("old_unmatched_br", region="US") is False
    assert ra.is_retraction_already_processed("old_unmatched_br", region="BR") is False


# ── 2026-10-27: Spotify's "withdrawn" wording (no "retracted" anywhere) ─────────────
WITHDRAWN_META = {
    "head_from": {"mail_address": "infringement-claim-response@spotify.com"},
    "date_formatted": "2026-10-26T15:00:00Z",
}
WITHDRAWN_BODY = (
    "Hi,\n\nThe claim has been withdrawn and the content will be reinstated within 48 hours. "
    "No strike will be applied.\n\nAffected Content\n\nLabel Name: Music Eve\n"
    "Artist: Israel Novaes, MusicEve, Maestro Pinocchio\nTitle: Comigo é Assim / Lapada Lapada\n"
    "UPC: 5064079652602\nURI: spotify:track:6cBNGZZVoF1OTaKbYzDyDJ\n"
)


def test_withdrawn_wording_is_a_retraction():
    assert ra.is_retraction_email(WITHDRAWN_BODY, meta=WITHDRAWN_META) is True
    # still requires the exact Spotify sender
    assert ra.is_retraction_email(WITHDRAWN_BODY, meta={"head_from": {"mail_address": "x@fuga.com"}}) is False
    # other phrasings stay detected / unrelated text stays ignored
    assert ra.is_retraction_email("The claimant has withdrawn the claim.", meta=WITHDRAWN_META) is True
    assert ra.is_retraction_email("Please keep the claim withdrawn from review.", meta=WITHDRAWN_META) is False


def test_withdrawn_email_fields_and_searches():
    entries = ra.extract_retraction_entries(WITHDRAWN_BODY, meta=WITHDRAWN_META)
    assert len(entries) == 1
    assert entries[0]["upc"] == "5064079652602"
    assert entries[0]["title"] == "Comigo é Assim / Lapada Lapada"
    assert entries[0]["spotify_uri"] == "spotify:track:6cBNGZZVoF1OTaKbYzDyDJ"
    assert {"retracted", "withdrawn"} <= set(ra.RETRACTION_SEARCH_QUERIES)


def test_withdrawn_email_listing_several_releases_yields_one_entry_per_upc():
    body = WITHDRAWN_BODY + (
        "\nLabel Name: Music Eve\nArtist: B\nTitle: Second Song\nUPC: 644401000933\n"
        "URI: spotify:track:7b8vUjIdQ4l7VfdBajSdoz\n"
    )
    entries = ra.extract_retraction_entries(body, meta=WITHDRAWN_META)
    assert [e["upc"] for e in entries] == ["5064079652602", "644401000933"]
    assert entries[1]["title"] == "Second Song"
    assert entries[1]["spotify_uri"] == "spotify:track:7b8vUjIdQ4l7VfdBajSdoz"


def test_withdrawn_email_is_never_a_new_claim():
    from copyright_alert.non_claim_email import non_claim_reason
    assert "retraction" in non_claim_reason(WITHDRAWN_BODY, "Infringement Claim Response")
    assert ra.extract_claim_entries(WITHDRAWN_BODY, "Infringement Claim Response", WITHDRAWN_META) == []


def test_retraction_pass_marks_every_upc_in_a_withdrawal_email(monkeypatch):
    body = WITHDRAWN_BODY + "\nLabel Name: X\nArtist: B\nTitle: Second\nUPC: 644401000933\nURI: spotify:track:abc\n"
    values = [
        ["UPC", "Title", "Artist", "Status", "ref_code", "Retracted"],
        ["5064079652602", "Comigo", "A", "🔍 Investigating", "", ""],
        ["644401000933", "Second", "B", "", "", ""],
    ]
    writes = []
    monkeypatch.setattr(dw, "search_inbox_messages", lambda q, **k: [{"message_id": "m1", "subject": "x"}] if q == "withdrawn" else [])
    monkeypatch.setattr(dw, "fetch_email", lambda mid: (body, WITHDRAWN_META))
    monkeypatch.setattr(dw, "is_retraction_already_processed", lambda *a, **k: False)
    monkeypatch.setattr(dw, "write_cell", lambda col, row, val: writes.append((col, row, val)) or True)
    saved = []
    monkeypatch.setattr(dw, "_save_retracted_claim", lambda mid, rec: saved.append((mid, rec)))
    monkeypatch.setattr(dw, "post_card", lambda *a, **k: (True, None))
    monkeypatch.setattr(dw, "ACTIVE_REGION", "BR")

    summary = dw.run_retraction_pass(values, notify=True)

    assert summary["candidates"] == 1 and summary["matched"] == 2 and summary["updated"] == 2
    assert {w[1] for w in writes} == {2, 3}
    assert saved and saved[-1][1]["matched"] is True and saved[-1][1]["upcs"] == ["5064079652602", "644401000933"]


def test_retraction_pass_leaves_email_open_when_an_upc_is_not_in_this_tracker(monkeypatch):
    body = WITHDRAWN_BODY + "\nLabel Name: X\nArtist: B\nTitle: Second\nUPC: 644401000933\nURI: spotify:track:abc\n"
    values = [["UPC", "Title", "Artist", "Status", "ref_code", "Retracted"],
              ["5064079652602", "Comigo", "A", "🔍 Investigating", "", ""]]
    monkeypatch.setattr(dw, "search_inbox_messages", lambda q, **k: [{"message_id": "m1", "subject": "x"}] if q == "withdrawn" else [])
    monkeypatch.setattr(dw, "fetch_email", lambda mid: (body, WITHDRAWN_META))
    monkeypatch.setattr(dw, "is_retraction_already_processed", lambda *a, **k: False)
    monkeypatch.setattr(dw, "write_cell", lambda *a, **k: True)
    saved = []
    monkeypatch.setattr(dw, "_save_retracted_claim", lambda mid, rec: saved.append((mid, rec)))
    monkeypatch.setattr(dw, "post_card", lambda *a, **k: (True, None))
    monkeypatch.setattr(dw, "ACTIVE_REGION", "BR")

    summary = dw.run_retraction_pass(values, notify=False)

    assert summary["matched"] == 1 and summary["unmatched"] == 1
    assert not any(rec.get("matched") for _, rec in saved)     # stays re-checkable


def test_retraction_pass_dry_run_writes_nothing(monkeypatch):
    values = [["UPC", "Title", "Artist", "Status", "ref_code", "Retracted"],
              ["5064079652602", "Comigo", "A", "🔍 Investigating", "", ""]]
    writes, saved, cards = [], [], []
    monkeypatch.setattr(dw, "search_inbox_messages", lambda q, **k: [{"message_id": "m1", "subject": "x"}] if q == "withdrawn" else [])
    monkeypatch.setattr(dw, "fetch_email", lambda mid: (WITHDRAWN_BODY, WITHDRAWN_META))
    monkeypatch.setattr(dw, "is_retraction_already_processed", lambda *a, **k: False)
    monkeypatch.setattr(dw, "write_cell", lambda *a, **k: writes.append(a) or True)
    monkeypatch.setattr(dw, "_save_retracted_claim", lambda mid, rec: saved.append(mid))
    monkeypatch.setattr(dw, "post_card", lambda *a, **k: cards.append(1) or (True, None))
    monkeypatch.setattr(dw, "ACTIVE_REGION", "BR")

    summary = dw.run_retraction_pass(values, notify=True, dry_run=True)

    assert summary["matched"] == 1 and summary["updated"] == 1      # reports what it WOULD do
    assert not writes and not saved and not cards


def test_mail_search_targets_the_shared_mailbox_not_me(monkeypatch):
    """Regression: user_mailbox_id "me" is the JWT user's personal mailbox -> 0 hits ever."""
    import json
    import subprocess
    import types

    seen = []

    def fake_run(cmd, *a, **k):
        seen.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout='{"items": [], "has_more": false}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(dw.lark_auth, "ensure_jwt_fresh", lambda *a, **k: True)
    assert dw.search_inbox_messages("withdrawn", sender="infringement-claim-response@spotify.com") == []
    params = json.loads(seen[0][seen[0].index("--params") + 1])
    assert params["user_mailbox_id"] == dw.MAILBOX == "soundon-copyright@bytedance.com"


def _fake_search(monkeypatch, payload):
    import json
    import subprocess
    import types
    monkeypatch.setattr(dw.lark_auth, "ensure_jwt_fresh", lambda *a, **k: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr=""))


_HIT = {"meta_data": {"message_biz_id": "m1", "title": "Claim withdrawn", "create_time": "2026-10-28T10:05:38Z"}}


def test_search_reads_the_bare_payload_shape(monkeypatch):
    _fake_search(monkeypatch, {"items": [_HIT], "has_more": False})
    assert [m["message_id"] for m in dw.search_inbox_messages("withdrawn")] == ["m1"]


def test_search_reads_the_wrapped_payload_shape(monkeypatch):
    """Regression: {"ok": true, "data": {"items": [...]}} used to parse as zero hits."""
    _fake_search(monkeypatch, {"ok": True, "data": {"items": [_HIT], "has_more": False}})
    hits = dw.search_inbox_messages("withdrawn")
    assert [m["message_id"] for m in hits] == ["m1"] and hits[0]["subject"] == "Claim withdrawn"


def test_search_finds_the_message_id_under_other_keys(monkeypatch):
    _fake_search(monkeypatch, {"items": [{"id": "x1", "meta_data": {"title": "t"}},
                                         {"message_id": "x2", "meta_data": {}}]})
    assert [m["message_id"] for m in dw.search_inbox_messages("withdrawn")] == ["x1", "x2"]


def test_search_logs_response_keys_when_it_finds_nothing(monkeypatch, capsys):
    _fake_search(monkeypatch, {"ok": True, "data": {"items": []}})
    assert dw.search_inbox_messages("withdrawn") == []
    assert "0 items; response keys=" in capsys.readouterr().out
