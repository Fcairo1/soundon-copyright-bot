from copyright_alert import daily_workflow as dw
from copyright_alert import persistent_callback as pc
from copyright_alert.dm_action_card import (
    DISMISS_ACTION,
    DISMISSED_STATUS_PREFIX,
    build_dismissed_card,
    build_dm_action_card,
)

HEADER = ["UPC", "Title", "Status", "Date Received", "Card Message ID", "Email Status"]
CASE = {
    "upc": "5063962570191", "isrc": "BRXXX2400001", "title": "T", "artist": "A", "claimant_name": "C",
    "claimant_email": "c@x.com", "dsp": "Spotify", "detected_at": "2026-10-01", "tracker_row": 3,
    "lark_card_message_id": "om_group_1", "region": "BR", "ref_id": "ref:_x:ref",
    "source_email_message_id": "srcmsg",
}


def _buttons(card):
    for el in card["elements"]:
        if el.get("tag") == "action":
            return el["actions"]
    return []


# --- the button -----------------------------------------------------------------------

def test_dm_card_has_dismiss_button_next_to_the_three_reply_buttons():
    buttons = _buttons(build_dm_action_card(CASE))
    assert [b["value"]["action"] for b in buttons] == ["spotify_reply"] * 3 + [DISMISS_ACTION]
    dismiss = buttons[-1]
    assert dismiss["text"]["content"] == "🚫 Dismiss"
    # carries what the handler needs to find the right tracker row
    assert dismiss["value"]["tracker_row"] == 3 and dismiss["value"]["upc"] == CASE["upc"]
    assert dismiss["value"]["lark_card_message_id"] == "om_group_1" and dismiss["value"]["region"] == "BR"


def test_dismissed_card_has_no_buttons():
    card = build_dismissed_card({"upc": "1", "artist": "A"}, "msg", ok=True)
    assert not _buttons(card) and "Dismissed" in card["header"]["title"]["content"]


# --- the click handler ------------------------------------------------------------------

def _wire(monkeypatch, rows, write_ok=True):
    values = [HEADER, *rows]
    writes, patches = [], []
    monkeypatch.setattr(pc, "_refresh_callback_credentials", lambda *a, **k: None)
    monkeypatch.setattr(pc, "read_sheet_values", lambda region=None: values)
    monkeypatch.setattr(pc, "update_sheet_email_status",
                        lambda row, val, region=None: writes.append((row, val, region)) or write_ok)
    monkeypatch.setattr(pc, "patch_message", lambda mid, card: patches.append((mid, card)))
    return writes, patches


VALUE = {"action": DISMISS_ACTION, "upc": CASE["upc"], "tracker_row": 2, "lark_card_message_id": "om_group_1",
         "region": "BR", "artist": "A"}


def test_dismiss_writes_dismissed_to_email_status_and_updates_the_card(monkeypatch):
    writes, patches = _wire(monkeypatch, [[CASE["upc"], "T", "", "2026-10-01", "om_group_1", ""]])
    pc._process_dismiss(dict(VALUE), "om_dm_1")
    assert len(writes) == 1 and writes[0][0] == 2 and writes[0][1].startswith(DISMISSED_STATUS_PREFIX)
    assert writes[0][1].endswith("BRT") and writes[0][2] == "BR"
    mid, card = patches[0]
    assert mid == "om_dm_1" and card["header"]["template"] == "grey" and not _buttons(card)


def test_dismiss_never_overwrites_an_existing_email_status(monkeypatch):
    writes, patches = _wire(monkeypatch, [[CASE["upc"], "T", "", "2026-10-01", "om_group_1", "Draft ✅ – agree – 2026-10-02 10:00 BRT"]])
    pc._process_dismiss(dict(VALUE), "om_dm_1")
    assert writes == []
    assert "Draft ✅ – agree" in str(patches[0][1]) and patches[0][1]["header"]["template"] == "red"


def test_dismiss_is_idempotent_when_already_dismissed(monkeypatch):
    writes, patches = _wire(monkeypatch, [[CASE["upc"], "T", "", "2026-10-01", "om_group_1", "Dismissed 🚫 – 2026-10-02 10:00 BRT"]])
    pc._process_dismiss(dict(VALUE), "om_dm_1")
    assert writes == [] and patches[0][1]["header"]["template"] == "grey"


def test_dismiss_reports_when_the_tracker_row_cannot_be_found(monkeypatch):
    writes, patches = _wire(monkeypatch, [["999999999999", "T", "", "2026-10-01", "om_other", ""]])
    pc._process_dismiss({**VALUE, "upc": "123456789012", "lark_card_message_id": "om_missing", "tracker_row": 40}, "om_dm_1")
    assert writes == [] and "Couldn't find" in str(patches[0][1])


def test_dismiss_reports_a_failed_write_instead_of_claiming_success(monkeypatch):
    writes, patches = _wire(monkeypatch, [[CASE["upc"], "T", "", "2026-10-01", "om_group_1", ""]], write_ok=False)
    pc._process_dismiss(dict(VALUE), "om_dm_1")
    assert "NOT dismissed" in str(patches[0][1]) and patches[0][1]["header"]["template"] == "red"


# --- daily DM step: dismissed cases stop getting the button -------------------------------

def test_daily_dm_step_skips_dismissed_cases_but_not_open_ones(monkeypatch):
    values = [HEADER,
              ["111111111111", "T1", "", "2026-09-01", "om_a", ""],                                   # open -> DM
              ["222222222222", "T2", "", "2026-09-01", "om_b", "Dismissed 🚫 – 2026-10-02 10:00 BRT"]]  # dismissed -> skip
    sent = []
    monkeypatch.setattr(dw, "send_dm_action_card", lambda case, *a, **k: sent.append(case["upc"]) or True)
    monkeypatch.setattr(dw, "_load_posted_claims_map", lambda: {"om_a": {"source_email_message_id": "s1"},
                                                                 "om_b": {"source_email_message_id": "s2"}})
    monkeypatch.setattr(dw, "_load_dm_state", lambda: {})
    monkeypatch.setattr(dw, "_save_dm_state", lambda state: None)
    result = dw.dm_action_cards(values)
    assert sent == ["111111111111"] and result["sent"] == 1


# --- group-card countdown: a dismissal must NOT freeze it, a recorded reply still does ----------

def test_countdown_keeps_refreshing_dismissed_cases_but_not_replied_ones(monkeypatch):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=2)).isoformat()
    header = ["UPC", "Title", "Status", "Date Received", "Lark Message ID", "Card Message ID", "Email Status"]
    values = [header,
              ["111111111111", "T1", "", recent, "om_a", "om_a", "Dismissed 🚫 – 2026-10-02 10:00 BRT"],
              ["222222222222", "T2", "", recent, "om_b", "om_b", "Draft ✅ – agree – 2026-10-02 10:00 BRT"],
              ["333333333333", "T3", "", recent, "om_c", "om_c", ""]]
    patched = []
    monkeypatch.setattr(dw, "load_posted_card", lambda mid: {"config": {}, "header": {}, "elements": []})
    monkeypatch.setattr(dw, "patch_card_message", lambda mid, card: patched.append(mid) or True)
    dw.countdown_refresh(values)
    assert sorted(patched) == ["om_a", "om_c"]
