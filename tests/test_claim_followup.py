from datetime import date

from copyright_alert import claim_followup as cf

TODAY = date(2026, 11, 10)
HEADER = ["SO Song ID", "SO Song Title", "Matched ISRC", "Matched Title", "SO Region", "Status", "Spotify"]
LINK = "https://open.spotify.com/track/" + "A" * 22


def filed(key_id, days_ago, **extra):
    row = {"so_song_id": key_id, "so_isrc": "X", "song": f"S{key_id}", "matched_title": f"M{key_id}",
           "matched_isrc": f"I{key_id}", "region": "BR", "filed_by": "Ana", "check_2d": "", "check_7d": "",
           "takedown_date": date.fromordinal(TODAY.toordinal() - days_ago).isoformat(), "_row": int(key_id) + 1}
    row.update(extra)
    row["key"] = f"{key_id}|I{key_id}"
    return row


def tabs(*rows):
    return {"Aug3": {"sheet_id": "s1", "rows": [HEADER] + [list(r) for r in rows]}}


def acr(key_id, status="Online", link=LINK, region="BR"):
    return [key_id, f"S{key_id}", f"I{key_id}", f"M{key_id}", region, status, link]


class FakeClient:
    def __init__(self, state):
        self.state, self.calls = state, 0

    def check(self, track_id, market):
        self.calls += 1
        return {"state": self.state, "detail": "x"}


def run(monkeypatch, rows, acr_rows, state="online", send_ok=True):
    writes, offline, sent = [], [], []
    monkeypatch.setattr(cf, "write_check", lambda r, c, v: writes.append((r, c, v)))
    monkeypatch.setattr(cf.sp, "record_offline", lambda cases, d: offline.append(cases[0]["key"]))
    monkeypatch.setattr(cf.sp, "write_cells", lambda cells: None)

    def send(region, card, log_context=""):
        sent.append((region, card))
        return {"ok": send_ok}
    client = FakeClient(state)
    res = cf.run_followups(today=TODAY, client=client, filed=rows, tabs=tabs(*acr_rows), send=send)
    return res, writes, offline, sent, client


def test_due_stage():
    assert cf.due_stage(filed("1", 1), TODAY) is None
    assert cf.due_stage(filed("1", 2), TODAY) == "check_2d"
    assert cf.due_stage(filed("1", 3, check_2d="Online 2026-11-09"), TODAY) is None
    assert cf.due_stage(filed("1", 7, check_2d="Online x"), TODAY) == "check_7d"
    assert cf.due_stage(filed("1", 9), TODAY) == "check_7d"          # skips a missed day-2 check
    assert cf.due_stage(filed("1", 9, check_7d="Offline x"), TODAY) is None
    assert cf.due_stage(filed("1", 9, takedown_date=""), TODAY) is None


def test_day2_offline_records_status(monkeypatch):
    res, writes, offline, sent, _ = run(monkeypatch, [filed("1", 2)], [acr("1")], state="offline")
    assert offline == ["1|I1"]
    assert writes == [(2, "check_2d", "Offline 2026-11-10")]
    assert sent == []


def test_day2_online_just_notes_it(monkeypatch):
    res, writes, offline, sent, _ = run(monkeypatch, [filed("1", 2)], [acr("1")])
    assert writes == [(2, "check_2d", "Online 2026-11-10")] and not offline and not sent


def test_day7_still_online_alerts_owner_once(monkeypatch):
    rows = [filed("1", 7), filed("2", 8, region="MX")]
    res, writes, _, sent, _ = run(monkeypatch, rows, [acr("1"), acr("2", region="MX")])
    assert sorted(r for r, _ in sent) == ["BR", "SPLA"]
    assert sorted(writes) == [(2, "check_7d", "Online 2026-11-10"), (3, "check_7d", "Online 2026-11-10")]


def test_failed_dm_leaves_day7_blank_for_retry(monkeypatch):
    res, writes, _, sent, _ = run(monkeypatch, [filed("1", 7)], [acr("1")], send_ok=False)
    assert writes == [] and res["alerts"][0]["sent"] is False


def test_unclear_answer_writes_nothing(monkeypatch):
    res, writes, *_ = run(monkeypatch, [filed("1", 2)], [acr("1")], state="unknown")
    assert writes == [] and res["unclear"] == 1


def test_already_offline_skips_spotify(monkeypatch):
    res, writes, _, _, client = run(monkeypatch, [filed("1", 7)], [acr("1", status="Offline")])
    assert client.calls == 0 and writes == [(2, "check_7d", "Offline 2026-11-10")]


def test_missing_case_or_link_is_written_once(monkeypatch):
    res, writes, *_ = run(monkeypatch, [filed("1", 2), filed("2", 2)], [acr("2", link="")])
    assert sorted(v.split(" 2026")[0] for _, _, v in writes) == ["No Spotify link", "Not in ACR sheet"]


def test_dry_run_changes_nothing(monkeypatch):
    writes = []
    monkeypatch.setattr(cf, "write_check", lambda *a: writes.append(a))
    res = cf.run_followups(today=TODAY, client=FakeClient("online"), filed=[filed("1", 7)], tabs=tabs(acr("1")),
                           send=lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")), dry_run=True)
    assert writes == [] and res["alerts"][0]["cases"] == 1


def test_nothing_due_needs_no_credentials(monkeypatch):
    monkeypatch.setattr(cf.sp.SpotifyClient, "from_env", classmethod(lambda cls: None))
    assert cf.run_followups(today=TODAY, filed=[filed("1", 1)])["due"] == 0
