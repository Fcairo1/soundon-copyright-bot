import json
from datetime import date

from copyright_alert import engagement_tracker as et
from copyright_alert import handle_callback as hc
from copyright_alert import tag_managers as tm
from copyright_alert.engagement_format import (
    format_compact,
    streams_info,
    streams_line,
    streams_suffix,
    tier_badge,
    trend_text,
)
from copyright_alert.run_alert import (
    STREAMS_ELEMENT_MARKER,
    apply_streams_to_card,
    build_card,
)


# --- formatting ---------------------------------------------------------------

def test_format_compact():
    assert format_compact(1_234_567) == "1.2M"
    assert format_compact("2000000") == "2M"
    assert format_compact(340_000) == "340K"
    assert format_compact(4_200) == "4.2K"
    assert format_compact(850.4) == "850"
    assert format_compact(999_700) == "1M"
    assert format_compact("N/A") == "—"
    assert format_compact(None) == "—"


def test_tier_badge_thresholds():
    assert tier_badge(250_000) == "🔥"
    assert tier_badge(12_000) == "⭐"
    assert tier_badge(900) == ""
    assert tier_badge("N/A") == ""


def test_trend_text():
    assert trend_text(1080, 1000) == "↑ 8% since claim"
    assert trend_text(880, 1000) == "↓ 12% since claim"
    assert trend_text(1004, 1000) == ""        # < 1% is noise
    assert trend_text(50, 0) == "↑ new"
    assert trend_text(50, None) == ""


def test_streams_line_only_shows_trend_after_a_real_refresh():
    snapshot_only = streams_info(now=None, baseline=1000, refreshed=False)
    assert streams_line(snapshot_only) == "**1K**"
    refreshed = streams_info(now=1500, baseline=1000, refreshed=True)
    assert streams_line(refreshed) == "**1.5K** · ↑ 50% since claim"
    assert streams_line(streams_info()) == "—"


def test_streams_suffix_marks_unknown_vs_low():
    assert streams_suffix(streams_info(now=1_200_000)) == " — 🎧 1.2M 🔥"
    assert streams_suffix(streams_info(now=300)) == " — 🎧 300"
    assert streams_suffix(None) == " — 🎧 —"


# --- group card ------------------------------------------------------------------

def _ef():
    return {"title": "Song", "upc": "123456789012", "isrc": "BRXXX2400001", "email_source": "Spotify",
            "claimant_name": "X", "dsp": "Spotify", "claimant_message": "msg", "date_received": "2026-10-07"}


def _streams_texts(card):
    return [json.dumps(el, ensure_ascii=False) for el in card["elements"]
            if STREAMS_ELEMENT_MARKER in json.dumps(el, ensure_ascii=False)]


def test_build_card_shows_spotify_streams_above_point_of_contact():
    card = build_card(_ef(), {"album_title": "Song", "sptf_30d_str": "1234567", "tt_30d_vv": "999"})
    texts = _streams_texts(card)
    assert len(texts) == 1 and "1.2M" in texts[0] and "🔥" in texts[0]
    tags = [json.dumps(el, ensure_ascii=False) for el in card["elements"]]
    streams_i = next(i for i, t in enumerate(tags) if STREAMS_ELEMENT_MARKER in t)
    poc_i = next(i for i, t in enumerate(tags) if "Point of Contact" in t)
    assert streams_i < poc_i
    assert "999" not in texts[0]  # Spotify only — TikTok isn't on the card


def test_build_card_without_engagement_shows_dash():
    card = build_card(_ef(), {"album_title": "Song"})
    assert "—" in _streams_texts(card)[0]


def test_apply_streams_replaces_instead_of_duplicating():
    card = build_card(_ef(), {"album_title": "Song", "sptf_30d_str": "1000"})
    apply_streams_to_card(card, streams_info(now=2000, baseline=1000, refreshed=True))
    apply_streams_to_card(card, streams_info(now=2000, baseline=1000, refreshed=True))
    texts = _streams_texts(card)
    assert len(texts) == 1 and "2K" in texts[0] and "↑ 100% since claim" in texts[0]


def test_apply_streams_adds_row_to_legacy_card_without_one():
    card = build_card(_ef(), {"album_title": "Song"})
    card["elements"] = [el for el in card["elements"] if STREAMS_ELEMENT_MARKER not in json.dumps(el, ensure_ascii=False)]
    assert not _streams_texts(card)
    apply_streams_to_card(card, streams_info(now=5000))
    assert len(_streams_texts(card)) == 1


# --- manager reminders ---------------------------------------------------------

HEADER = ["UPC", "Title", "Status", "Date Received", "Admin Action Taken", "BD", "Label Manager",
          "ISRC", "UID", "sptf_30d_str", "Spotify 30d Streams (Now)"]


def _collect(monkeypatch, rows):
    monkeypatch.setattr(tm, "resolve_managers_for_row", lambda row, idx: [("mariana.vieira", "Mariana Vieira")])
    monkeypatch.setattr(tm, "is_upc_excluded", lambda upc: False)
    return tm.collect_pending([HEADER, *rows])


def test_reminder_line_shows_refreshed_streams_with_badge(monkeypatch):
    managers, pending, no_mgr = _collect(monkeypatch, [
        ["638022638262", "Mundo", "", "2026-08-01", "", "", "", "", "", "50000", "1500000"],
    ])
    card = tm.build_tag_card(managers, no_mgr, region="BR", streams_by_upc=tm.streams_by_upc(pending))
    text = json.dumps(card, ensure_ascii=False)
    assert "🎧 1.5M 🔥" in text


def test_reminder_falls_back_to_at_claim_snapshot(monkeypatch):
    managers, pending, no_mgr = _collect(monkeypatch, [
        ["638022638262", "Mundo", "", "2026-08-01", "", "", "", "", "", "12000", ""],
    ])
    card = tm.build_tag_card(managers, no_mgr, region="BR", streams_by_upc=tm.streams_by_upc(pending))
    assert "🎧 12K ⭐" in json.dumps(card, ensure_ascii=False)


def test_reminder_unchanged_when_no_streams_passed(monkeypatch):
    managers, pending, no_mgr = _collect(monkeypatch, [
        ["638022638262", "Mundo", "", "2026-08-01", "", "", "", "", "", "12000", ""],
    ])
    assert "🎧" not in json.dumps(tm.build_tag_card(managers, no_mgr, region="BR"), ensure_ascii=False)


# --- daily refresh --------------------------------------------------------------

TRACKER_HEADER = ["UPC", "Status", "Admin Action Taken", "Retracted", "sptf_30d_str", "tt_30d_vv",
                  et.NOW_SPOTIFY_HEADER, et.NOW_TIKTOK_HEADER, et.CHECKED_HEADER]


def _patch_sheet(monkeypatch, rows):
    monkeypatch.setattr(hc, "_tracker_config", lambda region=None: ("https://sheet", "sid"))
    monkeypatch.setattr(hc, "read_sheet_values", lambda region=None: [TRACKER_HEADER, *rows])
    monkeypatch.setattr(et, "ensure_columns", lambda region: {"created": []})
    et.LATEST.clear()


def test_refresh_writes_now_columns_and_leaves_snapshot_alone(monkeypatch):
    _patch_sheet(monkeypatch, [
        ["111", "", "", "", "1000", "50", "", "", ""],
        ["222", "✅ Resolved", "", "", "1000", "50", "", "", ""],      # not open
        ["", "", "", "", "", "", "", "", ""],                            # blank row
    ])
    from copyright_alert import run_alert as ra
    queried = []
    monkeypatch.setattr(ra, "batch_query_engagement_by_upc",
                        lambda upcs: queried.append(list(upcs)) or {"111": {"sptf_30d_str": "2500.0", "tt_30d_vv": "75"}})
    writes = []
    import copyright_alert.lark_auth as lark_auth
    monkeypatch.setattr(lark_auth, "sheet_values_batch_update", lambda url, ranges: writes.extend(ranges))

    result = et.run_daily_sync("BR", is_open=lambda s, a, r: "Resolved" not in s, today=date(2026, 10, 7))

    assert queried == [["111"]]
    assert result["refreshed"] == 1 and result["open_rows"] == 1
    by_range = {w["range"]: w["values"][0][0] for w in writes}
    assert by_range == {"sid!G2:G2": 2500, "sid!H2:H2": 75, "sid!I2:I2": "2026-10-07"}
    assert not any(r.startswith(("sid!E", "sid!F")) for r in by_range)   # snapshot columns untouched
    assert et.LATEST["111"]["sptf_now"] == 2500


def test_refresh_keeps_old_values_when_upc_missing_from_aeolus(monkeypatch):
    _patch_sheet(monkeypatch, [["111", "", "", "", "1000", "50", "900", "40", "2026-10-06"]])
    from copyright_alert import run_alert as ra
    monkeypatch.setattr(ra, "batch_query_engagement_by_upc", lambda upcs: {})
    writes = []
    import copyright_alert.lark_auth as lark_auth
    monkeypatch.setattr(lark_auth, "sheet_values_batch_update", lambda url, ranges: writes.extend(ranges))
    result = et.run_daily_sync("BR", is_open=lambda s, a, r: True)
    assert writes == [] and result["refreshed"] == 0 and result["not_found_in_aeolus"] == 1


def test_refresh_checks_least_recently_checked_first_under_cap(monkeypatch):
    _patch_sheet(monkeypatch, [
        ["111", "", "", "", "1", "1", "", "", "2026-10-06"],
        ["222", "", "", "", "1", "1", "", "", ""],
        ["333", "", "", "", "1", "1", "", "", "2026-10-01"],
    ])
    monkeypatch.setattr(et, "MAX_REFRESH_PER_RUN", 2)
    from copyright_alert import run_alert as ra
    queried = []
    monkeypatch.setattr(ra, "batch_query_engagement_by_upc", lambda upcs: queried.append(list(upcs)) or {})
    result = et.run_daily_sync("BR", dry_run=True, is_open=lambda s, a, r: True)
    assert queried == [["222", "333"]] and result["pending_next_run"] == 1
