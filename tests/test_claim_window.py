"""Claim window: registration, always-TD account parsing, day-5 alarm."""
from datetime import date

import pytest

from copyright_alert import claim_window as cw

HEADER = ["SO Song ID", "SO Song Title", "SO Region", "Matched Title", "Matched ISRC",
          "Status", "Review Result", "SO User ID", "operation_manager_list"]
ACCT = "7110648495548269570"


def row(song_id, isrc, *, decision="Other Party", status="Online", user="1", region="BR", managers="Ana"):
    return [song_id, f"Song {song_id}", region, f"Match {isrc}", isrc, status, decision, user, managers]


def summarize(rows, no_claim=(), requested=(), accounts=()):
    return cw.summarize_cycle([HEADER] + rows, no_claim_keys=set(no_claim),
                              requested_keys=set(requested), always_td_ids=set(accounts))


# ── account list ────────────────────────────────────────────────────────────

def test_parse_account_ids_skips_sci_notation_short_and_dupes():
    grid = [[ACCT], ["7.11E+18"], ["12345"], [""], [], [ACCT], [" 7169574627113731073 "]]
    assert cw.parse_account_ids(grid) == [ACCT, "7169574627113731073"]


# ── per-region summary ──────────────────────────────────────────────────────

def test_only_online_other_party_counts():
    s = summarize([
        row("1", "A"),
        row("2", "B", decision="Own Release"),
        row("3", "C", status="Offline"),
    ])
    assert s["BR"]["to_claim"] == 1


def test_do_not_claim_and_requested_are_excluded():
    s = summarize([row("1", "A"), row("2", "B"), row("3", "C")],
                  no_claim={"1|A"}, requested={"2|B"})
    assert s["BR"]["to_claim"] == 1


def test_always_td_account_counted_separately_not_as_claim():
    s = summarize([row("1", "A", user=ACCT), row("2", "B")], accounts={ACCT})
    assert (s["BR"]["to_claim"], s["BR"]["always_td_left"]) == (1, 1)


def test_always_td_never_applies_to_own_release():
    s = summarize([row("1", "A", user=ACCT, decision="Own Release")], accounts={ACCT})
    assert (s["BR"]["to_claim"], s["BR"]["always_td_left"]) == (0, 0)


def test_always_td_wins_over_do_not_claim_mark():
    s = summarize([row("1", "A", user=ACCT)], no_claim={"1|A"}, accounts={ACCT})
    assert s["BR"]["always_td_left"] == 1


def test_duplicate_case_counted_once_and_managers_split():
    s = summarize([row("1", "A", managers="Ana"), row("1", "A", managers="Ana"),
                   row("2", "B", managers="Ana, Bia"), row("3", "C", managers="")])
    assert s["BR"]["to_claim"] == 3
    assert dict(s["BR"]["by_manager"]) == {"Ana": 2, "Bia": 1, "No manager on file": 1}


def test_regions_routed_by_country():
    s = summarize([row("1", "A", region="BR"), row("2", "B", region="MX"), row("3", "C", region="US")])
    # Country names are mapped by acr_digest.COUNTRY_TO_REGION
    for country, region in cw.ad.COUNTRY_TO_REGION.items():
        assert region in cw.REGIONS
    assert sum(v["to_claim"] for v in s.values()) == 3


# ── card ────────────────────────────────────────────────────────────────────

def test_card_shows_counts_and_managers():
    s = summarize([row("1", "A", managers="Ana"), row("2", "B", managers="Ana")])
    card = cw.build_alarm_card("BR", "Week 29", s["BR"])
    text = str(card)
    assert "Claim window closed" in text and "Week 29" in text and "**Ana** 2" in text


# ── registration ────────────────────────────────────────────────────────────

def test_register_window_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(cw, "read_windows", lambda: [{"cycle": "week 29"}])
    monkeypatch.setattr(cw, "_run_lark_sheets", lambda *a, **k: calls.append(a))
    res = cw.register_window("Week 29", date(2026, 10, 5))
    assert res["registered"] is False and not calls


def test_register_window_deadline_is_five_workdays(monkeypatch):
    sent = []
    monkeypatch.setattr(cw, "read_windows", lambda: [])
    monkeypatch.setattr(cw, "_run_lark_sheets", lambda args, input_text=None: sent.append(input_text))
    res = cw.register_window("Week 29", date(2026, 10, 8))   # Thursday
    assert res["deadline"] == "2026-10-15"                    # next Thursday, weekend skipped
    assert "Week 29" in sent[0] and "2026-10-15" in sent[0]


# ── alarm ───────────────────────────────────────────────────────────────────

@pytest.fixture
def alarm_env(monkeypatch):
    state = {"windows": [{"cycle": "Week 29", "posted_date": "2026-10-05", "deadline_date": "2026-10-12",
                          "alarmed_at": "", "_row": 2}], "sent": [], "written": [], "dm_ok": True}
    rows = [HEADER, row("1", "A"), row("2", "B", region="MX"), row("3", "C", user=ACCT)]
    monkeypatch.setattr(cw, "read_windows", lambda: state["windows"])
    monkeypatch.setattr(cw, "_key_sets", lambda: (set(), set()))
    monkeypatch.setattr(cw, "_tab_id", lambda url, name: "tabid")
    monkeypatch.setattr(cw, "_read_grid", lambda *a, **k: [[ACCT]])
    monkeypatch.setattr(cw.ad, "list_tabs", lambda url: [{"name": "Week 29", "sheet_id": "s1"}])
    monkeypatch.setattr(cw.ad, "read_tab", lambda url, sid: rows)
    monkeypatch.setattr(cw, "_set_alarmed", lambda w, text: state["written"].append(text))
    import copyright_alert.rights_confirmation_notice as rcn
    monkeypatch.setattr(rcn, "_send_to_ops",
                        lambda region, card, log_context="": (state["sent"].append(region) or {"ok": state["dm_ok"]}))
    return state


def test_alarm_not_sent_while_window_open(alarm_env):
    # Deadline Mon 12 Oct: still open on the deadline day itself.
    res = cw.run_daily_alarm(today=date(2026, 10, 12))
    assert res["checked"] == 0 and not alarm_env["sent"]


def test_alarm_sent_per_region_after_deadline(alarm_env):
    res = cw.run_daily_alarm(today=date(2026, 10, 13))
    assert alarm_env["sent"] == ["BR", "SPLA"]
    assert alarm_env["written"] and alarm_env["written"][0].startswith("DONE")
    assert res["checked"] == 1


def test_alarm_dry_run_sends_and_writes_nothing(alarm_env):
    res = cw.run_daily_alarm(today=date(2026, 10, 13), dry_run=True)
    assert not alarm_env["sent"] and not alarm_env["written"]
    assert res["sent"] and all(s["dry_run"] for s in res["sent"])


def test_alarm_retries_only_failed_regions(alarm_env):
    alarm_env["dm_ok"] = False
    cw.run_daily_alarm(today=date(2026, 10, 13))
    assert alarm_env["written"][0].startswith("PARTIAL")
    # Next run: BR already done, only the remaining regions are retried.
    alarm_env["windows"][0]["alarmed_at"] = "PARTIAL BR"
    alarm_env["sent"].clear()
    alarm_env["dm_ok"] = True
    cw.run_daily_alarm(today=date(2026, 10, 14))
    assert "BR" not in alarm_env["sent"]


def test_alarm_not_repeated_once_done(alarm_env):
    alarm_env["windows"][0]["alarmed_at"] = "DONE 2026-10-13 09:00 BR,US,SPLA"
    res = cw.run_daily_alarm(today=date(2026, 10, 14))
    assert res["checked"] == 0 and not alarm_env["sent"]


def test_run_daily_jobs_isolates_failures(monkeypatch):
    monkeypatch.setattr(cw, "sync_always_td_accounts", lambda dry_run=False: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(cw, "run_daily_alarm", lambda dry_run=False: {"checked": 0})
    out = cw.run_daily_jobs()
    assert "error" in out["always_td_sync"] and out["alarm"] == {"checked": 0}
