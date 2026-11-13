import json
from datetime import date, datetime, timedelta, timezone

import pytest

from copyright_alert import content_safety_report as csr

TODAY = date(2026, 11, 12)
HEADER = ["SO Song Title", "Review Result", "SO Song ID", "SO ISRC", "SO User ID", "SO Artist", "SO Region",
          "Spotify", "Streamings", "Matched Title", "Matched ISRC"]
T1, T2, T3 = "A" * 22, "B" * 22, "C" * 22


def _row(song, uid, region, tid, streams, isrc="ISRC1", matched="Copy", song_id="s1"):
    return [song, "", song_id, isrc, uid, '["Artist"]', region, f"https://open.spotify.com/track/{tid}",
            str(streams), matched, f"M-{tid}"]


def test_scan_date_parses_tab_names_without_year():
    assert csr.scan_date("Aug3", TODAY) == date(2026, 8, 3)
    assert csr.scan_date("Oct 15", TODAY) == date(2026, 10, 15)
    assert csr.scan_date("Dec 20", TODAY) == date(2025, 12, 20)       # in the future -> last year
    assert csr.scan_date("Flags", TODAY) is None


def test_rows_skip_utility_tabs_and_blank_rows():
    assert csr.rows_from_tab("Flags", [["foo"], ["bar"]], TODAY) == []
    rows = csr.rows_from_tab("Aug3", [HEADER, _row("Song", "u1", "BR", T1, 10), [""] * len(HEADER)], TODAY)
    assert len(rows) == 1 and rows[0]["scan"] == date(2026, 8, 3)


def test_build_tracks_dedupes_keeps_max_streams_and_majority_owner():
    rows = (csr.rows_from_tab("Aug3", [HEADER, _row("Song", "u1", "BR", T1, 100)], TODAY)
            + csr.rows_from_tab("Sep22", [HEADER, _row("Song", "u1", "BR", T1, 300), _row("Other", "u2", "BR", T1, 50, song_id="s2")], TODAY)
            + csr.rows_from_tab("Sep22", [HEADER, _row("Song", "u3", "MX", T2, 7, song_id="s3")], TODAY))
    tracks = csr.build_tracks(rows, excluded_track_ids=[T3])
    assert set(tracks) == {T1, T2}
    t = tracks[T1]
    assert t["streams"] == 300 and t["owner"] == "u1" and t["region"] == "BR"
    assert t["first"] == date(2026, 8, 3) and t["last"] == date(2026, 9, 22)
    assert tracks[T2]["region"] == "SPLA" and tracks[T2]["market"] == "MX"


def test_build_tracks_honours_exclusions_and_do_not_claim():
    rows = csr.rows_from_tab("Aug3", [HEADER, _row("Song", "u1", "BR", T1, 1), _row("Song", "u1", "BR", T2, 1)], TODAY)
    dnc = {csr.ad.case_key("s1", "Song", f"M-{T2}", "Copy")}
    assert set(csr.build_tracks(rows, excluded_track_ids=[T1], do_not_claim_keys=dnc)) == set()


def test_build_tracks_fills_missing_account_from_aeolus():
    rows = csr.rows_from_tab("Aug3", [HEADER, _row("Song", "", "BR", T1, 1, isrc="X1")], TODAY)
    assert csr.build_tracks(rows, isrc_to_user={"X1": "u9"})[T1]["owner"] == "u9"


class _Client:
    def __init__(self, states):
        self.states, self.calls = states, []

    def check(self, tid, market):
        self.calls.append((tid, market))
        return {"state": self.states.get(tid, "online")}


def test_availability_uses_cached_offline_and_rechecks_the_rest():
    tracks = {T1: {"market": "BR"}, T2: {"market": "US"}}
    cache = {T1: {"state": "offline"}}
    client = _Client({T2: "offline"})
    stats = csr.check_availability(tracks, client, cache, today=TODAY, sleep=lambda s: None)
    assert client.calls == [(T2, "US")]
    assert tracks[T1]["state"] == tracks[T2]["state"] == "offline"
    assert cache[T2]["state"] == "offline" and stats == {"cached_offline": 1, "offline": 1}


def _tracks():
    rows = csr.rows_from_tab("Sep22", [HEADER, _row("Song", "u1", "BR", T1, 1_000_000), _row("Hit", "u2", "US", T2, 2_000_000, isrc="I2"),
                                       _row("Song", "u1", "BR", T3, 5, isrc="ISRC1")], TODAY)
    tracks = csr.build_tracks(rows)
    for tid, s in ((T1, "offline"), (T2, "offline"), (T3, "online")):
        tracks[tid]["state"] = s
    return tracks


def test_summary_uses_real_pps_or_region_fallback():
    data = csr.summarize(_tracks(), {"accounts": {"u1": 0.002}, "region_fallback": {"US": 0.003}})
    assert data["found"] == 3 and data["down"] == 2 and data["streams"] == 3_000_000
    assert data["revenue"] == pytest.approx(2000 + 6000) and data["revenue_real"] == pytest.approx(2000)
    assert data["revenue_by_region"] == {"US": pytest.approx(6000), "BR": pytest.approx(2000)}
    assert data["accounts_protected"] == 2


def test_growth_plan_and_rows():
    tracks = _tracks()
    plan = csr.growth_plan(tracks, latest=date(2026, 11, 6))
    assert set(plan) == {"ISRC1", "I2"}
    p = plan["ISRC1"]
    assert p["est_td"] == date(2026, 10, 6) and p["before_d"] == date(2026, 10, 5) and p["after_d"] == date(2026, 10, 20)
    streams = {("ISRC1", "2026-10-05"): 100_000, ("ISRC1", "2026-10-20"): 130_000,
               ("I2", "2026-10-05"): 1_000_000, ("I2", "2026-10-20"): 1_100_000}
    rows = csr.growth_rows(plan, streams)
    assert [r["song"] for r in rows] == ["Hit", "Song"]          # ordered by extra streams
    assert rows[1]["copies"] == 1


def test_growth_skips_too_recent_and_small_songs():
    tracks = _tracks()
    assert csr.growth_plan(tracks, latest=date(2026, 10, 10)) == {}     # < 1 week after the takedown
    plan = csr.growth_plan(tracks, latest=date(2026, 11, 6))
    assert csr.growth_rows(plan, {("ISRC1", "2026-10-05"): 10, ("ISRC1", "2026-10-20"): 99}) == []


def test_card_shows_the_numbers():
    data = csr.summarize(_tracks(), {"accounts": {"u1": 0.002}, "region_fallback": {"US": 0.003}})
    card = csr.build_card(data, [], {"u1": "Label One"}, scans=[date(2026, 8, 3), date(2026, 10, 15)], checked=TODAY,
                          period_label="Nov 2026", button_url="https://x", previous={"down": 1})
    text = json.dumps(card, ensure_ascii=False)
    assert "3.0M" in text and "~$8.0K" in text and "2 infringing tracks taken down" in text
    assert "+1 since last report" in text and "Label One" in text and "(Aug–Oct 2026)" in text
    assert card["header"]["text_tag_list"][0]["text"]["content"] == "Nov 2026"


def test_donut_groups_small_accounts_per_region():
    accounts = [{"uid": "a", "region": "US", "revenue": 900}, {"uid": "b", "region": "US", "revenue": 10},
                {"uid": "c", "region": "US", "revenue": 5}, {"uid": "d", "region": "BR", "revenue": 85}]
    slices = csr.donut_slices(accounts, {"a": "A", "d": "D"})
    assert [s[0] for s in slices] == ["A", "2 other US", "D"]


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(csr, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(csr, "STATUS_CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr(csr, "load_config", lambda: {"enabled": True, "recipient": "me", "me_email": "x@y", "report_day": 1})
    fake = {"card": {"c": 1}, "data": {"found": 1, "down": 1, "streams": 1, "revenue": 1, "revenue_real": 1,
                                       "accounts_protected": 1, "down_by_region": {}, "unknown_status": 0},
            "growth": [], "status": {}, "cache": {"t": 1}, "latest_partition": TODAY}
    monkeypatch.setattr(csr, "build_report", lambda **kw: fake)
    sent = []
    monkeypatch.setattr(csr, "send_card", lambda card, cfg: sent.append(card) or {"ok": True, "to": "me"})
    return sent


def test_monthly_sends_once_per_month(_isolated):
    now = datetime(2026, 12, 1, 14, tzinfo=timezone.utc)
    assert csr.run_monthly(today=date(2026, 12, 1), client=object(), now=now)["sent"]["ok"]
    assert "already sent" in csr.run_monthly(today=date(2026, 12, 2), client=object(), now=now)["skipped"]
    assert csr.run_monthly(today=date(2027, 1, 1), client=object(), now=now)["sent"]["ok"]
    assert len(_isolated) == 2


def test_monthly_respects_a_run_in_progress(_isolated):
    now = datetime(2026, 12, 1, 14, tzinfo=timezone.utc)
    csr._write_json(csr.STATE_FILE, {"running_month": "2026-12", "running_since": (now - timedelta(hours=1)).isoformat()})
    assert "another run" in csr.run_monthly(today=date(2026, 12, 1), client=object(), now=now)["skipped"]
    csr._write_json(csr.STATE_FILE, {"running_month": "2026-12", "running_since": (now - timedelta(hours=9)).isoformat()})
    assert csr.run_monthly(today=date(2026, 12, 1), client=object(), now=now)["sent"]["ok"]


def test_dry_run_sends_nothing_and_writes_no_state(_isolated):
    out = csr.run_monthly(today=date(2026, 12, 1), client=object(), dry_run=True)
    assert out["dry_run"] and out["card"] == {"c": 1} and not _isolated
    assert not csr.STATE_FILE.exists()


def test_disabled_config_only_runs_when_forced(_isolated, monkeypatch):
    monkeypatch.setattr(csr, "load_config", lambda: {"enabled": False, "me_email": "x@y"})
    assert "disabled" in csr.run_monthly(today=date(2026, 12, 1), client=object())["skipped"]
    assert csr.run_monthly(today=date(2026, 12, 1), client=object(), force=True)["sent"]["ok"]
