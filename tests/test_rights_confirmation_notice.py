from copyright_alert import rights_confirmation_notice as rc

REAL_BODY = (
    "Hi, The below content has come to our attention. Please check, including with the "
    "artist/label where appropriate, to ensure SoundOn has all necessary rights to deliver "
    "the products below. While this claim is under investigation, this content will be "
    "removed from the service. Affected Content Label Name: Music Eve Artist: Israel Novaes, "
    "MusicEve, Maestro Pinocchio Title: Comigo é Assim / Lapada Lapada UPC: 5064079652602 "
    "URI: spotify:track:6cBNGZZVoF1OTaKbYzDyDJ If you believe this is incorrect and that you "
    "have all necessary rights to provide this content in accordance with our agreement with "
    "Spotify, please reply to this email (i) with an explanation... (ii) the sentence, "
    '"I state under penalty of perjury that I have the necessary rights to post the content '
    'found at spotify:track:6cBNGZZVoF1OTaKbYzDyDJ, "; (iii) the relevant contact details of '
    "the label. If we do not hear from you within five (5) business days, we will assume that "
    "you do not contest the takedown. Best regards, Spotify Content Protection "
    "ref:_00D0992XChO._500Qvj4AvB:ref"
)
REAL_SUBJECT = "Content Takedown - Music Eve - Claim 25238364 - ref:_00D0992XChO._500Qvj4AvB:ref"

HEADER = ["UPC", "User ID", "User Source", "BD", "Label Manager", "Status", "Date Received", "Card Message ID", "Email Status"]


def _row(upc, email_status=""):
    return [upc, "7613437133857473557", "AP", "", "", "Investigating", "2026-09-26", "om_x1", email_status]


def test_detects_real_content_takedown_email():
    assert rc.is_rights_confirmation_notice(REAL_BODY, REAL_SUBJECT) is True


def test_does_not_match_real_infringement_claim_subject():
    assert rc.is_rights_confirmation_notice(REAL_BODY, "Possibly Infringing - Notification Warning No 1 - X - Claim 1") is False


def test_does_not_match_metadata_notice_subject():
    assert rc.is_rights_confirmation_notice(REAL_BODY, "Takedown Notification - X - Claim 1") is False


def test_does_not_match_without_rights_phrase():
    assert rc.is_rights_confirmation_notice("some unrelated body", REAL_SUBJECT) is False


def test_matches_reply_subject_prefix_too():
    assert rc.is_rights_confirmation_notice(REAL_BODY, "Re: " + REAL_SUBJECT) is True


def test_parse_extracts_label_and_claim_number():
    fields = rc.parse_rights_confirmation_notice(REAL_BODY, REAL_SUBJECT)
    assert fields["upc"] == "5064079652602"
    assert fields["label"] == "Music Eve"
    assert fields["claim_number"] == "25238364"
    assert fields["spotify_uri"] == "spotify:track:6cBNGZZVoF1OTaKbYzDyDJ"


def test_notice_key_prefers_ref_id_then_claim_number():
    assert rc.notice_key({"ref_id": "ref:_abc"}) == "ref:_abc"
    assert rc.notice_key({"ref_id": "N/A", "claim_number": "123", "upc": "999"}) == "claim:123"
    assert rc.notice_key({"ref_id": "N/A", "claim_number": "N/A", "upc": "999"}) == "upc:999"


def test_append_row_default_status_and_bd_label_manager(monkeypatch):
    captured = {}

    monkeypatch.setattr(rc, "_ensure_rights_confirmation_sheet", lambda region: "sid")

    def fake_run_lark_sheets(args, input_text=None):
        if "+table-put" in args:
            import json
            captured["payload"] = json.loads(input_text)
        return {}

    monkeypatch.setattr(rc, "_run_lark_sheets", fake_run_lark_sheets)

    aeolus_row = {"uid": "u1", "source_type_name": "AP", "bd_manager_list": '["mariana.vieira"]',
                  "operation_manager_list": '["joao.silva"]'}
    fields = {"upc": "999", "date_received": "2026-09-26"}
    rc._append_rights_confirmation_row(fields, "BR", aeolus_row)

    row = captured["payload"]["sheets"][0]["data"][0]
    assert row[0] == "999"
    assert row[1] == "u1"
    assert row[2] == "AP"
    assert row[3] == "Mariana Vieira"
    assert row[4] == "Joao Silva"
    assert row[5] == rc.STATUS_INVESTIGATING


def test_resend_skips_already_answered_rows(monkeypatch):
    monkeypatch.setattr(rc, "_load_state", lambda: {"notices": {
        "k1": {"region": "BR", "upc": "111", "answered": False, "fields": {}, "last_dm_sent": ""},
    }})
    monkeypatch.setattr(rc, "_read_rights_tracker_rows", lambda region: (HEADER, [_row("111", email_status="Sent ✅")], "sid"))
    called = []
    monkeypatch.setattr(rc, "_send_rights_confirmation_dm", lambda fields, region: called.append(1) or {"ok": True, "message_id": "m1"})
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)

    summary = rc.resend_unanswered_notices()
    assert summary["skipped_answered"] == 1
    assert not called


def test_resend_sends_dm_when_email_status_blank(monkeypatch):
    monkeypatch.setattr(rc, "_load_state", lambda: {"notices": {
        "k1": {"region": "BR", "upc": "111", "answered": False, "fields": {"upc": "111"}, "last_dm_sent": ""},
    }})
    monkeypatch.setattr(rc, "_read_rights_tracker_rows", lambda region: (HEADER, [_row("111", email_status="")], "sid"))
    called = []
    monkeypatch.setattr(rc, "_send_rights_confirmation_dm", lambda fields, region: called.append(1) or {"ok": True, "message_id": "m1"})
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)

    summary = rc.resend_unanswered_notices()
    assert summary["resent"] == 1
    assert len(called) == 1


def test_resend_dedupes_within_same_day(monkeypatch):
    monkeypatch.setattr(rc, "_load_state", lambda: {"notices": {
        "k1": {"region": "BR", "upc": "111", "answered": False, "fields": {}, "last_dm_sent": rc._today_brt()},
    }})
    monkeypatch.setattr(rc, "_read_rights_tracker_rows", lambda region: (HEADER, [_row("111", email_status="")], "sid"))
    called = []
    monkeypatch.setattr(rc, "_send_rights_confirmation_dm", lambda fields, region: called.append(1) or {"ok": True})
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)

    summary = rc.resend_unanswered_notices()
    assert summary["skipped_today"] == 1
    assert not called


def test_group_card_has_blue_header_and_tags_managers():
    aeolus_row = {"bd_manager_list": '["mariana.vieira"]', "operation_manager_list": '["joao.silva"]'}
    card = rc.build_rights_confirmation_group_card(
        {"upc": "999", "title": "T", "artist": "A", "label": "L", "claim_number": "1", "date_received": "2026-09-26", "ref_id": "ref:1"},
        "BR", aeolus_row,
    )
    assert card["header"]["template"] == "blue"
    body_text = " ".join(el.get("text", {}).get("content", "") for el in card["elements"] if "text" in el)
    assert "mariana.vieira" in body_text
    assert "joao.silva" in body_text


def test_dm_card_has_two_buttons_with_distinct_choices():
    card = rc.build_rights_confirmation_dm_card({"upc": "999", "title": "T", "ref_id": "ref:1"}, "BR")
    actions = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
    choices = {a["value"]["choice"] for a in actions}
    assert choices == {"have_rights", "no_rights"}
    assert all(a["value"]["source"] == "dm" for a in actions)


def test_group_card_has_buttons_and_explanation_input_when_unresolved():
    aeolus_row = {"bd_manager_list": '["mariana.vieira"]', "operation_manager_list": ""}
    card = rc.build_rights_confirmation_group_card(
        {"upc": "999", "title": "T", "ref_id": "ref:1"}, "BR", aeolus_row)
    actions = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
    choices = {a["value"]["choice"] for a in actions}
    assert choices == {"have_rights", "no_rights"}
    assert all(a["value"]["source"] == "group" for a in actions)
    assert any(e.get("tag") == "input" and e.get("name") == "explanation" for e in card["elements"])


def test_group_card_hides_buttons_once_resolved():
    card = rc.build_rights_confirmation_group_card(
        {"upc": "999", "title": "T", "ref_id": "ref:1"}, "BR", {}, resolved_status="We have rights")
    assert not [e for e in card["elements"] if e.get("tag") == "action"]
    body_text = " ".join(el.get("text", {}).get("content", "") for el in card["elements"] if "text" in el)
    assert "We have rights" in body_text


def test_handle_reply_callback_have_rights_writes_status_and_marks_answered(monkeypatch):
    monkeypatch.setattr(rc, "get_notice", lambda key: {
        "fields": {"title": "T", "ref_id": "ref:1", "spotify_uri": "spotify:track:abc"},
        "source_email_message_id": "msg1",
    })
    sent = {}
    monkeypatch.setattr(rc, "_reply_have_rights", lambda *a, **k: sent.setdefault("called", True) or {"ok": True})
    updates = []
    monkeypatch.setattr(rc, "_set_email_status_and_status", lambda region, upc, es, st: updates.append((es, st)) or True)
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)
    monkeypatch.setattr(rc, "patch_card_message", lambda *a, **k: None)

    result = rc.handle_reply_callback({"choice": "have_rights", "key": "k1", "region": "BR", "upc": "999"},
                                       message_id="m1", explanation="we own it")
    assert sent.get("called")
    assert updates[0][1] == rc.STATUS_HAVE_RIGHTS
    assert result == "have_rights:k1:ok"


def test_handle_reply_callback_no_rights(monkeypatch):
    monkeypatch.setattr(rc, "get_notice", lambda key: {"fields": {"title": "T", "ref_id": "ref:1"}, "source_email_message_id": "msg1"})
    monkeypatch.setattr(rc, "_reply_no_rights", lambda *a, **k: {"ok": True})
    updates = []
    monkeypatch.setattr(rc, "_set_email_status_and_status", lambda region, upc, es, st: updates.append(st) or True)
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)
    monkeypatch.setattr(rc, "patch_card_message", lambda *a, **k: None)

    result = rc.handle_reply_callback({"choice": "no_rights", "key": "k1", "region": "BR", "upc": "999"})
    assert updates[0] == rc.STATUS_NO_RIGHTS
    assert result == "no_rights:k1:ok"


def test_handle_reply_callback_from_group_patches_both_cards_and_notifies_ops(monkeypatch):
    monkeypatch.setattr(rc, "get_notice", lambda key: {
        "fields": {"title": "T", "ref_id": "ref:1"},
        "source_email_message_id": "msg1",
        "aeolus_row": {"bd_manager_list": '["mariana.vieira"]'},
        "message_id": "dm_msg_1",       # the standing ops DM card
        "group_message_id": "group_msg_1",
    })
    monkeypatch.setattr(rc, "_reply_have_rights", lambda *a, **k: {"ok": True, "send_preview_url": "https://mail/draft1"})
    monkeypatch.setattr(rc, "_set_email_status_and_status", lambda *a, **k: True)
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)

    patched = {}
    monkeypatch.setattr(rc, "patch_card_message", lambda mid, card: patched.setdefault(mid, card))
    notified = {}
    monkeypatch.setattr(rc, "_send_to_ops", lambda region, card, **k: notified.setdefault("card", card) or {"ok": True})

    result = rc.handle_reply_callback(
        {"choice": "have_rights", "key": "k1", "region": "BR", "upc": "999", "source": "group"},
        message_id="group_msg_1", explanation="we own it",
    )

    assert set(patched.keys()) == {"dm_msg_1", "group_msg_1"}
    assert "We have rights" in notified["card"]["elements"][0]["text"]["content"]
    assert "https://mail/draft1" in notified["card"]["elements"][1]["text"]["content"]
    assert result == "have_rights:k1:ok"


def test_handle_reply_callback_from_dm_does_not_notify_ops(monkeypatch):
    monkeypatch.setattr(rc, "get_notice", lambda key: {
        "fields": {"title": "T", "ref_id": "ref:1"}, "source_email_message_id": "msg1",
        "aeolus_row": {}, "group_message_id": "group_msg_1",
    })
    monkeypatch.setattr(rc, "_reply_no_rights", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(rc, "_set_email_status_and_status", lambda *a, **k: True)
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)
    patched = {}
    monkeypatch.setattr(rc, "patch_card_message", lambda mid, card: patched.setdefault(mid, card))
    monkeypatch.setattr(rc, "_send_to_ops", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not notify ops")))

    rc.handle_reply_callback({"choice": "no_rights", "key": "k1", "region": "BR", "upc": "999", "source": "dm"},
                              message_id="dm_msg_1")
    # ops's own DM card (the one clicked) and the group card both get patched
    assert set(patched.keys()) == {"dm_msg_1", "group_msg_1"}


def test_handle_notice_skips_already_tracked(monkeypatch):
    monkeypatch.setattr(rc, "query_aeolus", lambda upc: {"user_region": "BR", "source_type_name": "AP"})
    monkeypatch.setattr(rc, "get_notice", lambda key: {"region": "BR"})
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)
    result = rc.handle_rights_confirmation_notice(REAL_BODY, REAL_SUBJECT, msg_id="m1")
    assert result["status"] == "already_tracked"


def test_handle_notice_falls_through_for_ineligible_source(monkeypatch):
    monkeypatch.setattr(rc, "query_aeolus", lambda upc: {"user_region": "BR", "source_type_name": "UG-Paid ads"})
    monkeypatch.setattr(rc, "_append_rights_confirmation_row",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write tracker row")))
    monkeypatch.setattr(rc, "post_rights_confirmation_group_card",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not post group card")))
    monkeypatch.setattr(rc, "_send_rights_confirmation_dm",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send DM")))

    result = rc.handle_rights_confirmation_notice(REAL_BODY, REAL_SUBJECT, msg_id="m1")
    assert result["status"] == "ineligible_source"
    assert result["source_type_name"] == "UG-Paid ads"


def test_handle_notice_proceeds_for_ar_source(monkeypatch):
    monkeypatch.setattr(rc, "query_aeolus", lambda upc: {"user_region": "BR", "source_type_name": "A&R"})
    monkeypatch.setattr(rc, "get_notice", lambda key: {})
    monkeypatch.setattr(rc, "_append_rights_confirmation_row", lambda *a, **k: True)
    monkeypatch.setattr(rc, "post_rights_confirmation_group_card", lambda *a, **k: (True, "msg1"))
    monkeypatch.setattr(rc, "_set_card_message_id", lambda *a, **k: True)
    monkeypatch.setattr(rc, "_send_rights_confirmation_dm", lambda *a, **k: {"ok": True, "message_id": "dm1"})
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)

    result = rc.handle_rights_confirmation_notice(REAL_BODY, REAL_SUBJECT, msg_id="m1")
    assert result["status"] == "new"
    assert result["group_ok"] is True


def test_find_notice_by_upc_returns_most_recent_match(monkeypatch):
    monkeypatch.setattr(rc, "_load_state", lambda: {"notices": {
        "old": {"upc": "111", "first_seen": "2026-09-01 00:00:00"},
        "new": {"upc": "111", "first_seen": "2026-09-20 00:00:00"},
        "other": {"upc": "222", "first_seen": "2026-09-25 00:00:00"},
    }})
    rec = rc.find_notice_by_upc("111")
    assert rec["first_seen"] == "2026-09-20 00:00:00"


def test_find_notice_by_upc_no_match_returns_empty(monkeypatch):
    monkeypatch.setattr(rc, "_load_state", lambda: {"notices": {}})
    assert rc.find_notice_by_upc("999") == {}


def test_handle_reply_callback_surfaces_thread_warning_on_card_and_state(monkeypatch):
    monkeypatch.setattr(rc, "get_notice", lambda key: {"fields": {"title": "T", "ref_id": "ref:1"}, "source_email_message_id": "msg1"})
    monkeypatch.setattr(rc, "_reply_have_rights", lambda *a, **k: {"ok": True, "thread_warning": "⚠️ Could NOT thread this reply..."})
    monkeypatch.setattr(rc, "_set_email_status_and_status", lambda *a, **k: True)
    captured_state = {}
    monkeypatch.setattr(rc, "update_json_state", lambda path, fn, **k: captured_state.setdefault("fn", fn))
    captured_card = {}
    monkeypatch.setattr(rc, "patch_card_message", lambda mid, card: captured_card.setdefault("card", card))

    rc.handle_reply_callback({"choice": "have_rights", "key": "k1", "region": "BR", "upc": "999"}, message_id="m1")

    body_text = " ".join(el.get("text", {}).get("content", "") for el in captured_card["card"]["elements"] if "text" in el)
    assert "Could NOT thread" in body_text

    state = {"notices": {"k1": {}}}
    captured_state["fn"](state)
    assert state["notices"]["k1"]["thread_warning"] == "⚠️ Could NOT thread this reply..."


def test_handle_reply_callback_failed_draft_does_not_mark_resolved(monkeypatch):
    # Real bug reported 2026-09-29: an expired Lark Mail JWT made
    # _reply_have_rights fail, and the uncaught path made the whole callback
    # thread crash silently — from the operator's side, clicking looked like
    # it did nothing at all. A failure must be visible and retryable, never
    # silently treated as resolved.
    monkeypatch.setattr(rc, "get_notice", lambda key: {
        "fields": {"title": "T", "ref_id": "ref:1"}, "source_email_message_id": "msg1",
        "aeolus_row": {}, "message_id": "dm_msg_1", "group_message_id": "group_msg_1",
    })
    monkeypatch.setattr(rc, "_reply_have_rights", lambda *a, **k: {
        "ok": False, "error": "LarkMailDraftError('lark-cli +reply did not return a draft_id: {}')"})
    monkeypatch.setattr(rc, "_set_email_status_and_status",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write tracker on failure")))
    monkeypatch.setattr(rc, "update_json_state",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not mark resolved on failure")))
    patched = {}
    monkeypatch.setattr(rc, "patch_card_message", lambda mid, card: patched.setdefault(mid, card))

    result = rc.handle_reply_callback(
        {"choice": "have_rights", "key": "k1", "region": "BR", "upc": "999", "source": "dm"}, message_id="dm_msg_1")

    assert result == "have_rights:k1:failed"
    assert set(patched.keys()) == {"dm_msg_1", "group_msg_1"}
    # The failure notice is prepended but the real buttons must still be
    # there — the operator needs to be able to retry once the underlying
    # issue (e.g. an expired token) is fixed.
    dm_card = patched["dm_msg_1"]
    body_text = " ".join(el.get("text", {}).get("content", "") for el in dm_card["elements"] if "text" in el)
    assert "Could not create the reply draft" in body_text
    assert "draft_id" in body_text  # the real error detail is surfaced, not swallowed
    actions = [e for e in dm_card["elements"] if e.get("tag") == "action"]
    assert actions and {a["value"]["choice"] for a in actions[0]["actions"]} == {"have_rights", "no_rights"}


def test_resolved_dm_card_shows_review_and_send_draft_button():
    # Real gap reported 2026-09-29: the resolved card showed a checkmark but
    # no way to actually open the draft — unlike the normal infringement-
    # claim flow's "🔗 Review & Send Draft" button.
    card = rc.build_rights_confirmation_dm_card(
        {"upc": "999", "title": "T", "ref_id": "ref:1"}, "BR",
        resolved_status="We have rights", draft_url="https://mail.example/draft1",
    )
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions
    btn = actions[0]["actions"][0]
    assert btn["url"] == "https://mail.example/draft1"
    assert "Review & Send Draft" in btn["text"]["content"]


def test_resolved_dm_card_warns_when_no_draft_url():
    card = rc.build_rights_confirmation_dm_card(
        {"upc": "999", "title": "T", "ref_id": "ref:1"}, "BR", resolved_status="We have rights")
    assert not [e for e in card["elements"] if e.get("tag") == "action"]
    body_text = " ".join(el.get("text", {}).get("content", "") for el in card["elements"] if "text" in el)
    assert "drafts folder manually" in body_text


def test_resolved_group_card_shows_review_and_send_draft_button():
    card = rc.build_rights_confirmation_group_card(
        {"upc": "999", "title": "T", "ref_id": "ref:1"}, "BR", {},
        resolved_status="We have rights", draft_url="https://mail.example/draft1",
    )
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions and actions[0]["actions"][0]["url"] == "https://mail.example/draft1"


def test_handle_reply_callback_success_passes_draft_url_to_patched_cards(monkeypatch):
    monkeypatch.setattr(rc, "get_notice", lambda key: {
        "fields": {"title": "T", "ref_id": "ref:1"}, "source_email_message_id": "msg1",
        "aeolus_row": {}, "message_id": "dm_msg_1", "group_message_id": "group_msg_1",
    })
    monkeypatch.setattr(rc, "_reply_have_rights", lambda *a, **k: {"ok": True, "send_preview_url": "https://mail.example/draft1"})
    monkeypatch.setattr(rc, "_set_email_status_and_status", lambda *a, **k: True)
    monkeypatch.setattr(rc, "update_json_state", lambda *a, **k: None)
    patched = {}
    monkeypatch.setattr(rc, "patch_card_message", lambda mid, card: patched.setdefault(mid, card))
    monkeypatch.setattr(rc, "_send_to_ops", lambda *a, **k: {"ok": True})

    rc.handle_reply_callback({"choice": "have_rights", "key": "k1", "region": "BR", "upc": "999", "source": "dm"},
                              message_id="dm_msg_1")

    for card in patched.values():
        actions = [e for e in card["elements"] if e.get("tag") == "action"]
        assert actions and actions[0]["actions"][0]["url"] == "https://mail.example/draft1"
