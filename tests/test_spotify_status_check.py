"""Weekly Spotify status check: classification, planning, throttling."""
import json
from datetime import date

import pytest

from copyright_alert import spotify_status_check as sc

HEADER = ["SO Song ID", "SO Song Title", "SO Region", "Matched Title", "Matched ISRC", "Spotify",
          "Status", "Review Result"]
TID = "7MY80DSHHAA4M7V7Ku4Bfk"


def row(song_id, isrc, link=f"https://open.spotify.com/track/{TID}", status="Online", decision="Other Party", region="BR"):
    return [song_id, f"Song {song_id}", region, f"Match {isrc}", isrc, link, status, decision]


def tabs_of(*rows):
    return {"Aug3": {"sheet_id": "S1", "rows": [HEADER, *rows]}}


# ── helpers ─────────────────────────────────────────────────────────────────
def test_track_id_from_link_and_uri():
    assert sc.track_id_from_link(f"https://open.spotify.com/track/{TID}?si=abc") == TID
    assert sc.track_id_from_link(f"spotify:track:{TID}") == TID
    assert sc.track_id_from_link("https://open.spotify.com/album/xyz") == ""
    assert sc.track_id_from_link("") == ""


@pytest.mark.parametrize("region,market", [("BR", "BR"), (" mx ", "MX"), ("", "US"), ("Brazil", "US")])
def test_market_for(region, market):
    assert sc.market_for(region) == market


def test_col_letter():
    assert [sc._col_letter(i) for i in (0, 25, 26, 56)] == ["A", "Z", "AA", "BE"]


# ── planning ────────────────────────────────────────────────────────────────
def test_plan_only_online_other_party_with_a_track_link():
    plan = sc.plan_checks(tabs_of(
        row("1", "A"),
        row("2", "B", status="Offline"),
        row("3", "C", decision="Own Release"),
        row("4", "D", link=""),
        row("5", "E", link="https://open.spotify.com/album/x"),
    ))
    assert len(plan) == 1
    (track, market), entry = next(iter(plan.items()))
    assert (track, market) == (TID, "BR") and list(entry["cases"]) == ["1|A"]
    assert entry["cells"] == [("S1", "G", 2)]   # Status is the 7th column, data starts at sheet row 2


def test_plan_dedupes_same_track_and_market_across_rows():
    plan = sc.plan_checks(tabs_of(row("1", "A"), row("2", "B")))
    assert len(plan) == 1 and len(next(iter(plan.values()))["cases"]) == 2
    assert [c[2] for c in next(iter(plan.values()))["cells"]] == [2, 3]


def test_plan_uses_each_cases_own_market():
    plan = sc.plan_checks(tabs_of(row("1", "A", region="BR"), row("2", "B", region="MX")))
    assert {m for (_, m) in plan} == {"BR", "MX"}


# ── classification ──────────────────────────────────────────────────────────
class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, data, headers):
        self.calls.append(url)
        if "accounts.spotify.com" in url:
            return 200, {}, json.dumps({"access_token": "tok"})
        market = url.rsplit("market=", 1)[1]
        status, body = self.routes[market] if market in self.routes else self.routes["*"]
        return status, {}, json.dumps(body) if isinstance(body, dict) else body


def client(routes):
    http = FakeHttp(routes)
    return sc.SpotifyClient("id", "secret", http=http, sleep=lambda s: None), http


def test_404_is_offline():
    c, _ = client({"*": (404, {"error": {}})})
    assert c.check(TID, "BR")["state"] == "offline"


def test_playable_is_online_with_a_single_call():
    c, http = client({"*": (200, {"is_playable": True})})
    assert c.check(TID, "BR")["state"] == "online"
    assert len([u for u in http.calls if "/tracks/" in u]) == 1


def test_unplayable_in_region_but_playable_elsewhere_stays_online():
    c, _ = client({"BR": (200, {"is_playable": False}), "US": (200, {"is_playable": True})})
    v = c.check(TID, "BR")
    assert v["state"] == "online" and "US" in v["detail"]


def test_unplayable_in_both_markets_is_offline_and_us_rechecks_with_br():
    c, http = client({"*": (200, {"is_playable": False, "restrictions": {"reason": "market"}})})
    assert c.check(TID, "US")["state"] == "offline"
    assert http.calls[-1].endswith("market=BR")


def test_server_errors_are_unknown_never_offline():
    c, _ = client({"*": (500, "boom")})
    assert c.check(TID, "BR")["state"] == "unknown"


def test_rate_limit_retries_then_succeeds():
    seq = iter([(429, {"Retry-After": "1"}, ""), (200, {}, json.dumps({"is_playable": True}))])
    slept = []

    def http(url, data, headers):
        if "accounts.spotify.com" in url:
            return 200, {}, json.dumps({"access_token": "t"})
        return next(seq)

    c = sc.SpotifyClient("i", "s", http=http, sleep=slept.append)
    assert c.check(TID, "BR")["state"] == "online" and slept


def test_expired_token_is_refreshed_once():
    calls = {"tok": 0}
    seq = iter([401, 200])

    def http(url, data, headers):
        if "accounts.spotify.com" in url:
            calls["tok"] += 1
            return 200, {}, json.dumps({"access_token": f"t{calls['tok']}"})
        return next(seq), {}, json.dumps({"is_playable": True})

    c = sc.SpotifyClient("i", "s", http=http, sleep=lambda s: None)
    assert c.check(TID, "BR")["state"] == "online" and calls["tok"] == 2


# ── run ─────────────────────────────────────────────────────────────────────
def test_run_without_credentials_is_a_noop(monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    assert "skipped" in sc.run_check(tabs=tabs_of(row("1", "A")))


def test_dry_run_changes_nothing_but_reports(monkeypatch):
    monkeypatch.setattr(sc, "record_offline", lambda *a: pytest.fail("must not write"))
    monkeypatch.setattr(sc, "write_cells", lambda *a: pytest.fail("must not write"))
    c, _ = client({"*": (404, {})})
    res = sc.run_check(dry_run=True, client=c, tabs=tabs_of(row("1", "A")), sleep=lambda s: None)
    assert len(res["offline"]) == 1 and res["offline"][0]["detail"].startswith("removed")


def test_offline_result_is_logged_and_cells_written(monkeypatch):
    logged, cells = [], []
    monkeypatch.setattr(sc, "record_offline", lambda cases, detail: logged.append((cases, detail)))
    monkeypatch.setattr(sc, "write_cells", lambda c: cells.append(c) or {"written": len(c), "failed": 0})
    c, _ = client({"*": (404, {})})
    sc.run_check(client=c, tabs=tabs_of(row("1", "A"), row("2", "B")), sleep=lambda s: None)
    assert [x["key"] for x in logged[0][0]] == ["1|A", "2|B"] and "BR" in logged[0][1]
    assert cells[0] == [("S1", "G", 2), ("S1", "G", 3)]


def test_online_tracks_are_not_written(monkeypatch):
    monkeypatch.setattr(sc, "record_offline", lambda *a: pytest.fail("must not write"))
    c, _ = client({"*": (200, {"is_playable": True})})
    res = sc.run_check(client=c, tabs=tabs_of(row("1", "A")), sleep=lambda s: None)
    assert res["online"] == 1 and res["offline"] == []


def test_record_offline_row_matches_status_changes_columns(monkeypatch):
    sent = []
    monkeypatch.setattr(sc, "_run_lark_sheets", lambda args, input_text=None: sent.append(json.loads(input_text)))
    sc.record_offline([{"key": "1|A", "so_song_id": "1", "matched_isrc": "A", "song": "S", "matched_title": "M", "region": "BR"}], "removed")
    sheet = sent[0]["sheets"][0]
    r = dict(zip(sheet["columns"], sheet["data"][0]))
    assert sheet["name"] == "Status Changes" and sheet["mode"] == "append"
    assert (r["old_status"], r["new_status"], r["source"], r["changed_by"]) == ("Online", "Offline", "auto", "Spotify check")


# ── weekly throttle ─────────────────────────────────────────────────────────
@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(sc, "run_check", lambda **k: {"checked": 1, "online": 1, "offline": [], "unknown": 0, "dry_run": k.get("dry_run", False)})
    return tmp_path / "state.json"


def test_runs_when_never_run_then_waits_a_week(state):
    assert "skipped" not in sc.run_weekly(today=date(2026, 10, 12))
    assert json.loads(state.read_text())["last_run"] == "2026-10-12"
    assert "skipped" in sc.run_weekly(today=date(2026, 10, 18))
    assert "skipped" not in sc.run_weekly(today=date(2026, 10, 19))


def test_force_overrides_the_wait_and_dry_run_never_saves_state(state):
    sc.run_weekly(today=date(2026, 10, 12))
    assert "skipped" not in sc.run_weekly(today=date(2026, 10, 13), force=True)
    state.unlink()
    sc.run_weekly(today=date(2026, 10, 13), dry_run=True)
    assert not state.exists()
