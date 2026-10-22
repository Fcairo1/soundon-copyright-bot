import json
from datetime import date, timedelta

import pytest

from copyright_alert import daily_workflow as dw
from copyright_alert import run_alert as ra
from copyright_alert import tag_managers as tm
from copyright_alert.dm_action_card import build_dm_action_card
from copyright_alert.engagement_format import streams_info

SONY = {"tier": 1, "major": "Sony Music Entertainment", "arm": "recorded"}


def ef(email="alexis.masciarella@sonymusic.com", name="Alexis Sony Music Entertainment"):
    return {"title": "Carona", "upc": "5064079630013", "isrc": "BK9M02600084", "email_source": "Spotify",
            "claimant_name": name, "claimant_email": email, "dsp": "Spotify", "claimant_message": "m",
            "date_received": "2026-09-28 17:09"}


AR = {"album_title": "Carona", "display_artist": '["Cesinha"]', "uid": "1",
      "bd_manager_list": '["eduardo.praca"]', "operation_manager_list": '["felipe.pierro"]'}


def text(card):
    return json.dumps(card, ensure_ascii=False)


# --- group card ---------------------------------------------------------------------------

def test_sony_claim_gets_the_loud_header_and_banner():
    card = ra.build_card(ef(), AR)
    assert card["header"]["template"] == "carmine"
    assert card["header"]["title"]["content"] == "🚨 MAJOR LABEL CLAIM — Sony Music Entertainment"
    assert text(card).count(ra.MAJOR_BANNER_MARKER) == 1 and "Tier 1" in text(card)


@pytest.mark.parametrize("email,name", [("legal@believe.com", "Believe"),            # tier 2
                                        ("someone@gmail.com", "Some Artist"),         # tier 3
                                        ("rights@soundon.global", "SoundOn")])        # tier 4 (internal)
def test_non_major_claims_keep_the_normal_card(email, name):
    card = ra.build_card(ef(email, name), AR)
    assert (card["header"]["template"], card["header"]["title"]["content"]) == ra.CARD_HEADER_DEFAULT
    assert ra.MAJOR_BANNER_MARKER not in text(card)


def test_banner_sits_above_the_case_summary():
    card = ra.build_card(ef(), AR)
    elements = [json.dumps(e, ensure_ascii=False) for e in card["elements"]]
    banner_i = next(i for i, t in enumerate(elements) if ra.MAJOR_BANNER_MARKER in t)
    summary_i = next(i for i, t in enumerate(elements) if "Artist(s)" in t)
    assert banner_i < summary_i


def test_major_header_wins_over_high_streaming_but_both_banners_show():
    card = ra.build_card(ef(), {**AR, "sptf_30d_str": "1104268"})
    assert card["header"]["template"] == "carmine"
    assert ra.MAJOR_BANNER_MARKER in text(card) and ra.HIGH_BANNER_MARKER in text(card)


def test_daily_streams_refresh_does_not_wipe_the_major_header():
    card = ra.build_card(ef(), AR)
    ra.apply_streams_to_card(card, streams_info(now=200_000, baseline=150_000, refreshed=True))
    assert card["header"]["template"] == "carmine"
    assert text(card).count(ra.MAJOR_BANNER_MARKER) == 1
    ra.apply_streams_to_card(card, streams_info(now=10, baseline=20, refreshed=True))
    assert card["header"]["template"] == "carmine"


def test_tier_style_is_idempotent_and_reverts_when_no_longer_major():
    card = ra.build_card(ef("someone@gmail.com", "Artist"), AR)
    ra.apply_tier_style_to_card(card, SONY)
    ra.apply_tier_style_to_card(card, SONY)
    assert text(card).count(ra.MAJOR_BANNER_MARKER) == 1 and card["header"]["template"] == "carmine"
    ra.apply_tier_style_to_card(card, {"tier": 3})
    assert ra.MAJOR_BANNER_MARKER not in text(card)
    assert (card["header"]["template"], card["header"]["title"]["content"]) == ra.CARD_HEADER_DEFAULT


# --- separate group ping ----------------------------------------------------------------------

def test_major_claim_ping_mentions_bd_and_label_manager(monkeypatch):
    posted = []
    monkeypatch.setattr(ra, "post_card", lambda card, aeolus_row=None, **k: posted.append((card, k)) or (True, "m1"))
    assert ra.notify_major_label_claim(ef(), AR, "BR", "claim_msg") is True
    card, kwargs = posted[0]
    assert card["header"]["title"]["content"] == "🏛️ Major label claim — Sony Music Entertainment"
    body = text(card)
    assert "5064079630013" in body and "<at " in body and "please review this claim today" in body
    assert kwargs["expected_region"] == "BR"


@pytest.mark.parametrize("email", ["legal@believe.com", "someone@gmail.com"])
def test_no_ping_for_non_major_claims(monkeypatch, email):
    monkeypatch.setattr(ra, "post_card", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no ping")))
    assert ra.notify_major_label_claim(ef(email, "X"), AR, "BR") is False


def test_a_failed_ping_never_raises(monkeypatch):
    monkeypatch.setattr(ra, "post_card", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lark down")))
    assert ra.notify_major_label_claim(ef(), AR, "BR") is False


# --- ops DM card ---------------------------------------------------------------------------------

CASE = {"upc": "5064079630013", "isrc": "BK9M02600084", "title": "Carona", "artist": "Cesinha",
        "claimant_name": "Alexis Sony Music Entertainment", "claimant_email": "alexis.masciarella@sonymusic.com",
        "dsp": "Spotify", "detected_at": "2026-09-28", "region": "BR"}


def test_dm_card_for_a_major_claim_is_loud():
    card = build_dm_action_card(CASE)
    assert card["header"]["template"] == "carmine" and card["header"]["title"]["content"].startswith("🏛️ MAJOR LABEL")
    assert "MAJOR LABEL CLAIM — Sony Music Entertainment" in text(card)


def test_dm_card_for_other_claims_is_unchanged():
    card = build_dm_action_card({**CASE, "claimant_email": "x@gmail.com", "claimant_name": "X"})
    assert card["header"]["template"] == "orange" and "MAJOR" not in text(card)


def test_dm_card_trusts_the_tracker_tier_when_given():
    assert build_dm_action_card({**CASE, "claimant_tier": "3"})["header"]["template"] == "orange"
    assert build_dm_action_card({**CASE, "claimant_email": "N/A", "claimant_name": "N/A",
                                 "claimant_tier": "1", "claimant_group": "Warner Music Group"})["header"]["template"] == "carmine"


def test_major_claim_dm_goes_out_on_day_zero_but_other_claims_wait_a_day(monkeypatch):
    today = date.today().isoformat() + " 08:00"
    header = ["UPC", "Title", "Status", "Date Received", "Card Message ID", "Email Status", "Claimant Tier"]
    values = [header,
              ["111111111111", "Major", "", today, "om_a", "", "1"],
              ["222222222222", "Indie", "", today, "om_b", "", "3"]]
    sent = []
    monkeypatch.setattr(dw, "send_dm_action_card", lambda case, *a, **k: sent.append(case["upc"]) or True)
    monkeypatch.setattr(dw, "_load_posted_claims_map", lambda: {"om_a": {"source_email_message_id": "s1"},
                                                                 "om_b": {"source_email_message_id": "s2"}})
    monkeypatch.setattr(dw, "_load_dm_state", lambda: {})
    monkeypatch.setattr(dw, "_save_dm_state", lambda state: None)
    dw.dm_action_cards(values)
    assert sent == ["111111111111"]


# --- countdown refresh restyles persisted cards from the tracker's tier ------------------------------

def test_countdown_refresh_adds_the_major_style_to_a_persisted_card(monkeypatch):
    recent = (date.today() - timedelta(days=2)).isoformat()
    header = ["UPC", "Title", "Status", "Date Received", "Lark Message ID", "Card Message ID", "Email Status",
              "Claimant Tier", "Claimant Group", "Claimant Arm"]
    values = [header, ["5064079630013", "Carona", "", recent, "om_a", "om_a", "", "1", "Sony Music Entertainment", "recorded"]]
    plain = ra.build_card(ef("someone@gmail.com", "Artist"), AR)        # a card posted before this feature
    patched = {}
    monkeypatch.setattr(dw, "load_posted_card", lambda mid: plain)
    monkeypatch.setattr(dw, "patch_card_message", lambda mid, card: patched.update({mid: card}) or True)
    dw.countdown_refresh(values)
    assert patched["om_a"]["header"]["template"] == "carmine" and ra.MAJOR_BANNER_MARKER in text(patched["om_a"])


# --- manager reminders ---------------------------------------------------------------------------------

def test_reminder_lists_majors_first_with_a_tag_and_a_callout(monkeypatch):
    monkeypatch.setattr(tm, "today_brt", lambda: date(2026, 10, 21))
    managers = {"m": {"display": "M", "items": [("old", "Old Overdue", "2026-08-01"),
                                                 ("sony", "Sony Track", "2026-10-19")]}}
    card = tm.build_tag_card(managers, [], region="BR", streams_by_upc={}, tier_by_upc={"old": 3, "sony": 1},
                             group_by_upc={"sony": "Sony Music Entertainment"})
    body = text(card)
    assert body.index("Sony Track") < body.index("Old Overdue")          # major first despite a fresher deadline
    assert "MAJOR · Sony" in body and "1 major label claim**" in body


def test_reminder_without_majors_has_no_major_callout(monkeypatch):
    monkeypatch.setattr(tm, "today_brt", lambda: date(2026, 10, 21))
    managers = {"m": {"display": "M", "items": [("a", "Alpha", "2026-08-01")]}}
    card = tm.build_tag_card(managers, [], region="BR", streams_by_upc={}, tier_by_upc={"a": 3}, group_by_upc={})
    assert "major label" not in text(card).lower()


def test_collect_pending_carries_tier_and_group(monkeypatch):
    header = ["UPC", "Title", "Status", "Date Received", "Admin Action Taken", "Claimant Tier", "Claimant Group"]
    monkeypatch.setattr(tm, "resolve_managers_for_row", lambda row, idx: [("m", "M")])
    monkeypatch.setattr(tm, "is_upc_excluded", lambda upc: False)
    _, pending, _ = tm.collect_pending([header, ["5064079630013", "Carona", "", "2026-09-28", "", "1", "Sony Music Entertainment"]])
    assert tm.tier_by_upc(pending) == {"5064079630013": 1}
    assert tm.group_by_upc(pending) == {"5064079630013": "Sony Music Entertainment"}
