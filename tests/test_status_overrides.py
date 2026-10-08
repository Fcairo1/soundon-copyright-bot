"""The bot sees the same Online/Offline the dashboard shows (Status Changes tab)."""
from copyright_alert import acr_digest as ad
from copyright_alert import claim_window as cw

HEADER = ["SO Song ID", "SO Song Title", "SO Region", "Matched Title", "Matched ISRC",
          "Status", "Review Result", "SO User ID", "operation_manager_list", "Streamings"]


def row(song_id, isrc, status="Online", decision="Other Party", managers="Ana"):
    return [song_id, f"Song {song_id}", "BR", f"Match {isrc}", isrc, status, decision, "1", managers, "100"]


def test_apply_overrides_changes_only_matching_cases():
    rows = [HEADER, row("1", "A"), row("2", "B")]
    out = ad.apply_status_overrides(rows, {"1|A": {"status": "Offline", "old_status": "Online"}})
    assert [r[5] for r in out[1:]] == ["Offline", "Online"]
    assert rows[1][5] == "Online"           # input grid untouched


def test_override_ignored_when_cell_was_edited_to_a_different_value_since():
    rows = [HEADER, row("1", "A", status="Offline")]
    ov = {"1|A": {"status": "Online", "old_status": "Offline"}}
    assert ad.apply_status_overrides(rows, ov)[1][5] == "Online"            # equals old: applies
    rows2 = [HEADER, row("1", "A", status="Online")]
    ov2 = {"1|A": {"status": "Offline", "old_status": "Offline"}}
    assert ad.apply_status_overrides(rows2, ov2)[1][5] == "Online"          # sheet moved on


def test_blank_status_cell_gets_the_override():
    rows = [HEADER, row("1", "A", status="")]
    assert ad.apply_status_overrides(rows, {"1|A": {"status": "Offline", "old_status": ""}})[1][5] == "Offline"


def test_read_overrides_last_change_wins_and_fails_open(monkeypatch):
    monkeypatch.undo()   # drop the conftest stub for this module's real function
    grid = [["key", "new_status", "old_status"], ["1|A", "Offline", "Online"], ["1|A", "online", "Offline"], ["2|B", "Offline", "Online"]]
    monkeypatch.setattr(ad, "list_tabs", lambda url: [{"name": "Status Changes", "sheet_id": "s"}])
    monkeypatch.setattr(ad, "read_tab", lambda url, sid: grid)
    assert ad.read_status_overrides() == {"1|A": {"status": "Online", "old_status": "Offline"},
                                          "2|B": {"status": "Offline", "old_status": "Online"}}
    monkeypatch.setattr(ad, "list_tabs", lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    assert ad.read_status_overrides() == {}


def test_corrected_offline_case_drops_out_of_the_claim_count():
    rows = [HEADER, row("1", "A"), row("2", "B")]
    s = cw.summarize_cycle(ad.apply_status_overrides(rows, {"1|A": {"status": "Offline", "old_status": "Online"}}),
                           no_claim_keys=set(), requested_keys=set(), always_td_ids=set())
    assert s["BR"]["to_claim"] == 1
