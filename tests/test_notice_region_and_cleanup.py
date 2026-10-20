import json

import pytest

from copyright_alert import metadata_notice as mn
from copyright_alert import notice_tab_cleanup as nc
from copyright_alert import rights_confirmation_notice as rc
from copyright_alert import run_alert as ra


def acct(region, source="AP", uid="1"):
    return {"user_region": region, "source_type_name": source, "uid": uid}


# --- strict region resolution ----------------------------------------------------------

def test_indonesian_account_is_out_of_region_not_filed_under_the_current_region(monkeypatch):
    monkeypatch.setattr(ra, "CURRENT_REGION", "BR")
    monkeypatch.setattr(ra, "batch_query_aeolus_rows_by_upc", lambda upcs: {"8997238506645": [acct("ID")]})
    region, row, status = ra.resolve_notice_region("8997238506645", acct("ID"))
    assert (region, status) == ("", "out_of_region") and row["user_region"] == "ID"


@pytest.mark.parametrize("country,expected", [("BR", "BR"), ("US", "US"), ("CA", "US"), ("MX", "SPLA"), ("CL", "SPLA")])
def test_ams_accounts_route_to_their_own_tracker(monkeypatch, country, expected):
    monkeypatch.setattr(ra, "CURRENT_REGION", "BR")
    assert ra.resolve_notice_region("1", acct(country))[0::2] == (expected, "ok")


def test_upc_under_two_accounts_is_routed_by_its_ams_account(monkeypatch):
    # Real case: 1200220155316 exists as a GB A&R account AND a US UG-SEO account.
    monkeypatch.setattr(ra, "batch_query_aeolus_rows_by_upc",
                        lambda upcs: {"1200220155316": [acct("GB", "A&R", "a"), acct("US", "UG-SEO", "b")]})
    region, row, status = ra.resolve_notice_region("1200220155316", acct("GB", "A&R", "a"), {"AP", "A&R"})
    assert (region, status, row["uid"]) == ("US", "ok", "b")


def test_preferred_source_wins_among_ams_accounts(monkeypatch):
    monkeypatch.setattr(ra, "batch_query_aeolus_rows_by_upc",
                        lambda upcs: {"1": [acct("US", "Organic", "x"), acct("BR", "AP", "y")]})
    assert ra.resolve_notice_region("1", acct("US", "Organic", "x"), {"AP", "A&R"})[0] == "BR"


def test_unknown_upc_reports_no_row(monkeypatch):
    monkeypatch.setattr(ra, "batch_query_aeolus_rows_by_upc", lambda upcs: {})
    assert ra.resolve_notice_region("1", {})[0::2] == ("", "no_aeolus_row")


def test_rights_handler_drops_a_foreign_account_without_touching_trackers_or_cards(monkeypatch):
    monkeypatch.setattr(rc, "query_aeolus", lambda upc: acct("ID"))
    monkeypatch.setattr(rc, "resolve_notice_region", lambda *a, **k: ("", acct("ID"), "out_of_region"))
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write/post/DM for a foreign account"))  # noqa: E731
    for name in ("_append_rights_confirmation_row", "post_rights_confirmation_group_card", "_send_rights_confirmation_dm"):
        monkeypatch.setattr(rc, name, boom)
    monkeypatch.setattr(rc, "parse_rights_confirmation_notice",
                        lambda body, subject, meta: {"upc": "8997238506645", "ref_id": "ref:_x:ref", "subject": subject})
    result = rc.handle_rights_confirmation_notice("body", "Content Takedown", {}, msg_id="m1")
    assert result["status"] == "out_of_region" and result["user_region"] == "ID"


def test_metadata_handler_drops_a_foreign_account(monkeypatch):
    monkeypatch.setattr(mn, "query_aeolus", lambda upc: acct("ID"))
    monkeypatch.setattr(mn, "resolve_notice_region", lambda *a, **k: ("", acct("ID"), "out_of_region"))
    monkeypatch.setattr(mn, "parse_metadata_notice",
                        lambda body, subject, meta: {"upc": "8997238506645", "ref_id": "ref:_y:ref", "subject": subject})
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write/DM for a foreign account"))  # noqa: E731
    monkeypatch.setattr(mn, "_append_metadata_correction_row", boom)
    monkeypatch.setattr(mn, "_send_notice_dm", boom)
    result = mn.handle_metadata_notice("body", "Takedown Notification", {}, msg_id="m1")
    assert result["status"] == "out_of_region" and result["user_region"] == "ID"


# --- idempotent appends ----------------------------------------------------------------

def test_rights_append_skips_an_identical_existing_row(monkeypatch):
    header = rc.RIGHTS_CONFIRMATION_HEADERS
    existing = ["010735111780", "u", "AP", "", "", "Investigating", "2026-09-19 08:09", "", ""]
    monkeypatch.setattr(rc, "_ensure_rights_confirmation_sheet", lambda region: "sid")
    monkeypatch.setattr(rc, "_read_rights_tracker_rows", lambda region: (header, [existing], "sid"))
    monkeypatch.setattr(rc, "_run_lark_sheets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no duplicate append")))
    assert rc._append_rights_confirmation_row({"upc": "010735111780", "date_received": "2026-09-19 08:09"}, "BR", {}) is True


def test_rights_append_still_appends_a_new_notice_for_the_same_upc_on_another_date(monkeypatch):
    header = rc.RIGHTS_CONFIRMATION_HEADERS
    existing = ["010735111780", "u", "AP", "", "", "Investigating", "2026-09-19 08:09", "", ""]
    calls = []
    monkeypatch.setattr(rc, "_ensure_rights_confirmation_sheet", lambda region: "sid")
    monkeypatch.setattr(rc, "_read_rights_tracker_rows", lambda region: (header, [existing], "sid"))
    monkeypatch.setattr(rc, "_run_lark_sheets", lambda args, input_text=None: calls.append(args) or {})
    rc._append_rights_confirmation_row({"upc": "010735111780", "date_received": "2026-10-20 09:00"}, "BR", {})
    assert calls and calls[0][0] == "+table-put"


def test_metadata_append_skips_an_identical_existing_row(monkeypatch):
    header = mn.METADATA_CORRECTIONS_HEADERS
    row = ["5063971200997", "2026-07-27 05:34"] + [""] * (len(header) - 2)
    monkeypatch.setattr(mn, "_ensure_metadata_corrections_sheet", lambda region: "sid")
    monkeypatch.setattr(mn, "_ensure_streams_column", lambda region, **k: {})
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (header, [row], "sid"))
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no duplicate append")))
    assert mn._append_metadata_correction_row({"upc": "5063971200997", "date_received": "2026-07-27 05:34"}, "BR") is True


# --- cleanup planning ------------------------------------------------------------------

H = rc.RIGHTS_CONFIRMATION_HEADERS


def r(upc, uid="u", date="2026-10-10 17:42", status="Investigating", card="", email=""):
    return [upc, uid, "AP", "", "", status, date, card, email]


def region_map(table):
    return lambda tab, upc, uid: table.get(upc, "?")


def test_plan_deletes_foreign_rows_moves_other_ams_rows_and_keeps_own(monkeypatch):
    tabs = {"BR": {"header": H, "rows": [(2, r("8997238506645")), (3, r("5064079921081")), (4, r("010735111780"))]},
            "US": {"header": H, "rows": [(2, r("7316472696423"))]}, "SPLA": {"header": H, "rows": []}}
    plan = nc.plan_cleanup(tabs, region_map({"8997238506645": None, "5064079921081": "US",
                                             "010735111780": "BR", "7316472696423": "US"}))
    actions = {(e["tab"], e["row"]): e["action"] for e in plan["entries"]}
    assert actions == {("BR", 2): "delete_foreign", ("BR", 3): "move", ("BR", 4): "keep", ("US", 2): "keep"}


def test_plan_leaves_unresolvable_rows_alone():
    tabs = {"BR": {"header": H, "rows": [(2, r("111"))]}, "US": {"header": H, "rows": []}, "SPLA": {"header": H, "rows": []}}
    assert nc.plan_cleanup(tabs, region_map({}))["entries"][0]["action"] == "keep_unknown"


def test_plan_collapses_duplicates_keeping_the_row_with_progress():
    rows = [(n, r("010735111780")) for n in range(2, 8)]
    rows.append((8, r("010735111780", status="We have rights", card="om_1", email="Sent")))
    tabs = {"BR": {"header": H, "rows": rows}, "US": {"header": H, "rows": []}, "SPLA": {"header": H, "rows": []}}
    plan = nc.plan_cleanup(tabs, region_map({"010735111780": "BR"}))
    kept = [e["row"] for e in plan["entries"] if e["action"] == "keep"]
    assert kept == [8] and plan["summary"]["delete_duplicate"] == 6


def test_plan_prefers_the_copy_already_in_the_right_tab_over_moving():
    tabs = {"BR": {"header": H, "rows": [(2, r("5064079921081", date="2026-09-19 08:09"))]},
            "US": {"header": H, "rows": [(2, r("5064079921081", date="2026-09-19 08:09"))]},
            "SPLA": {"header": H, "rows": []}}
    plan = nc.plan_cleanup(tabs, region_map({"5064079921081": "US"}))
    actions = {(e["tab"], e["row"]): e["action"] for e in plan["entries"]}
    assert actions == {("BR", 2): "delete_duplicate", ("US", 2): "keep"}          # no pointless move


def test_plan_never_touches_protected_rows():
    tabs = {"BR": {"header": H, "rows": [(2, r("8997238506645"))]}, "US": {"header": H, "rows": []}, "SPLA": {"header": H, "rows": []}}
    plan = nc.plan_cleanup(tabs, region_map({"8997238506645": None}), protected={"BR": {2}})
    assert plan["entries"][0]["action"] == "protected"


def test_same_upc_on_different_dates_are_not_duplicates():
    tabs = {"BR": {"header": H, "rows": [(2, r("111", date="2026-09-01")), (3, r("111", date="2026-10-01"))]},
            "US": {"header": H, "rows": []}, "SPLA": {"header": H, "rows": []}}
    assert [e["action"] for e in nc.plan_cleanup(tabs, region_map({"111": "BR"}))["entries"]] == ["keep", "keep"]


# --- cleanup execution (fake sheets) -------------------------------------------------------

def _wire_cleanup(monkeypatch, tmp_path, tabs_rows, region_table):
    state = {}
    calls = []

    def fake_read(region):
        rows = [r_ for _n, r_ in tabs_rows[region]]
        return H, rows, f"sid_{region}"

    def fake_run(args, input_text=None):
        calls.append((args, input_text))
        if args[0] == "+csv-get":
            region = args[args.index("--url") + 1].rsplit("/", 1)[-1]
            import csv, io
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(H)
            for n, row_ in tabs_rows[region]:
                w.writerow(row_)
            return {"data": {"annotated_csv": buf.getvalue()}}
        return {}

    monkeypatch.setattr(rc, "_read_rights_tracker_rows", fake_read)
    monkeypatch.setattr(rc, "_rights_tracker_url", lambda region: f"https://x/{region}")
    monkeypatch.setattr(rc, "_run_lark_sheets", fake_run)
    monkeypatch.setattr(nc, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(nc, "batch_query_aeolus_rows_by_upc",
                        lambda upcs: {u: [{"user_region": region_table[u], "uid": "u", "source_type_name": "AP"}]
                                      for u in upcs if u in region_table})
    monkeypatch.setattr(rc, "STATE_FILE", str(tmp_path / "state.json"))
    (tmp_path / "state.json").write_text(json.dumps({"notices": {
        "k1": {"upc": "8997238506645", "region": "BR", "answered": False},
        "k2": {"upc": "5064079921081", "region": "BR", "answered": False},
        "k3": {"upc": "010735111780", "region": "BR", "answered": False}}}))
    return calls


def test_dry_run_reports_but_changes_nothing(monkeypatch, tmp_path):
    rows = {"BR": [(2, r("8997238506645")), (3, r("010735111780")), (4, r("010735111780"))],
            "US": [], "SPLA": []}
    calls = _wire_cleanup(monkeypatch, tmp_path, rows, {"8997238506645": "ID", "010735111780": "BR"})
    out = nc.run_cleanup("rights", apply=False)
    assert out["summary"]["delete_foreign"] == 1 and out["summary"]["delete_duplicate"] == 1
    assert [c[0][0] for c in calls] == ["+csv-get"] * 3          # reads only
    assert list(tmp_path.glob("notice_tab_cleanup_*")) == []        # no backup written for a dry run


def test_apply_backs_up_moves_deletes_and_updates_state(monkeypatch, tmp_path):
    rows = {"BR": [(2, r("8997238506645")), (3, r("5064079921081")), (4, r("010735111780")), (5, r("010735111780"))],
            "US": [], "SPLA": []}
    calls = _wire_cleanup(monkeypatch, tmp_path, rows, {"8997238506645": "ID", "5064079921081": "US", "010735111780": "BR"})
    out = nc.run_cleanup("rights", apply=True)

    assert "backup" in out and (tmp_path / out["backup"].rsplit("/", 1)[-1]).exists()
    ops = [c[0][0] for c in calls]
    assert ops.index("+table-put") < ops.index("+dim-delete")                # move (append) BEFORE any delete
    put = json.loads(next(c[1] for c in calls if c[0][0] == "+table-put"))
    assert put["sheets"][0]["data"][0][0] == "5064079921081"
    assert next(c[0] for c in calls if c[0][0] == "+table-put")[2] == "https://x/US"   # appended to the US tracker
    delete = next(c[0] for c in calls if c[0][0] == "+dim-delete")
    assert json.loads(delete[delete.index("--ranges") + 1]) == ["2:2", "3:3", "5:5"]    # foreign, moved, duplicate
    assert "--yes" in delete
    state = json.loads((tmp_path / "state.json").read_text())["notices"]
    assert state["k1"]["answered"] is True and state["k1"]["closed_reason"] == "out_of_region"
    assert state["k2"]["region"] == "US" and state["k3"]["region"] == "BR" and state["k3"]["answered"] is False


# --- resend loops stop for notices whose tab row was removed ---------------------------------

def _state_recorder(monkeypatch, module, notices):
    state = {"notices": json.loads(json.dumps(notices))}
    monkeypatch.setattr(module, "_load_state", lambda: state)
    monkeypatch.setattr(module, "update_json_state", lambda path, fn, default=None: fn(state))
    return state


def test_rights_resend_closes_a_notice_whose_row_was_removed_instead_of_dmming_forever(monkeypatch):
    state = _state_recorder(monkeypatch, rc, {"k1": {"upc": "8997238506645", "region": "BR", "tracker_row_written": True,
                                                    "fields": {}, "last_dm_sent": ""}})
    monkeypatch.setattr(rc, "_read_rights_tracker_rows", lambda region: (rc.RIGHTS_CONFIRMATION_HEADERS, [], "sid"))
    monkeypatch.setattr(rc, "_send_rights_confirmation_dm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no DM")))
    summary = rc.resend_unanswered_notices()
    assert state["notices"]["k1"]["answered"] is True and state["notices"]["k1"]["closed_reason"] == "row_removed"
    assert summary["resent"] == 0


def test_rights_resend_still_nags_when_the_row_exists_and_is_unanswered(monkeypatch):
    row = ["8997238506645", "u", "AP", "", "", "Investigating", "2026-10-10", "", ""]
    _state_recorder(monkeypatch, rc, {"k1": {"upc": "8997238506645", "region": "BR", "tracker_row_written": True,
                                            "fields": {"upc": "8997238506645"}, "last_dm_sent": ""}})
    monkeypatch.setattr(rc, "_read_rights_tracker_rows", lambda region: (rc.RIGHTS_CONFIRMATION_HEADERS, [row], "sid"))
    sent = []
    monkeypatch.setattr(rc, "_send_rights_confirmation_dm", lambda f, region: sent.append(region) or {"ok": True})
    assert rc.resend_unanswered_notices()["resent"] == 1 and sent == ["BR"]


def test_rights_resend_does_not_close_a_notice_whose_row_was_never_written(monkeypatch):
    # tracker append failed originally -> the DM nag is the only visibility; keep it
    _state_recorder(monkeypatch, rc, {"k1": {"upc": "1", "region": "BR", "tracker_row_written": False,
                                            "fields": {"upc": "1"}, "last_dm_sent": ""}})
    monkeypatch.setattr(rc, "_read_rights_tracker_rows", lambda region: (rc.RIGHTS_CONFIRMATION_HEADERS, [], "sid"))
    sent = []
    monkeypatch.setattr(rc, "_send_rights_confirmation_dm", lambda f, region: sent.append(1) or {"ok": True})
    assert rc.resend_unanswered_notices()["resent"] == 1


def test_metadata_resend_closes_a_notice_whose_row_was_removed(monkeypatch):
    state = _state_recorder(monkeypatch, mn, {"k1": {"region": "BR", "tracker_row_written": True, "resolved": False,
                                                    "fields": {"upc": "8997238506645"}, "last_dm_sent": ""}})
    monkeypatch.setattr(mn, "_open_statuses_by_region", lambda regions: {"BR": {"5063971200997": True}})
    monkeypatch.setattr(mn, "_send_notice_dm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no DM")))
    mn.resend_unresolved_notices()
    assert state["notices"]["k1"]["resolved"] is True and state["notices"]["k1"]["resolved_by"] == "row_removed"


def test_metadata_resend_unaffected_when_the_region_tab_could_not_be_read(monkeypatch):
    _state_recorder(monkeypatch, mn, {"k1": {"region": "BR", "tracker_row_written": True, "resolved": False,
                                            "fields": {"upc": "1"}, "last_dm_sent": ""}})
    monkeypatch.setattr(mn, "_open_statuses_by_region", lambda regions: {})      # read failed -> no entry for BR
    sent = []
    monkeypatch.setattr(mn, "_send_notice_dm", lambda f, region: sent.append(1) or {"ok": True})
    mn.resend_unresolved_notices()
    assert sent == [1]


# --- a failed Aeolus chunk must be retried, and an unresolved UPC reported -------------------

def test_aeolus_rows_lookup_retries_a_failed_chunk(monkeypatch):
    calls = []
    good = {"columns": ["upc", "user_region", "p_date"], "rows": [["111", "ID", "2026-10-20"]]}

    def fake_sql(sql, timeout=0, dataset_id=None):
        calls.append(1)
        return None if len(calls) < 3 else good            # two failures, then success

    monkeypatch.setattr(ra, "_run_aeolus_sql", fake_sql)
    monkeypatch.setattr(ra, "_friendly_aeolus_rows",
                        lambda parsed: [dict(zip(parsed["columns"], r)) for r in parsed["rows"]])
    monkeypatch.setattr(ra.time, "sleep", lambda s: None)
    out = ra.batch_query_aeolus_rows_by_upc(["111"])
    assert len(calls) == 3 and out["111"][0]["user_region"] == "ID"


def test_aeolus_rows_lookup_gives_up_after_three_attempts_without_raising(monkeypatch):
    monkeypatch.setattr(ra, "_run_aeolus_sql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ra.time, "sleep", lambda s: None)
    assert ra.batch_query_aeolus_rows_by_upc(["111"]) == {}


def test_cleanup_warns_about_unresolved_upcs_and_leaves_them_alone(monkeypatch, tmp_path):
    rows = {"BR": [(2, r("8997238506645")), (3, r("111111111111"))], "US": [], "SPLA": []}
    _wire_cleanup(monkeypatch, tmp_path, rows, {"8997238506645": "ID"})     # 111111111111 not resolvable
    out = nc.run_cleanup("rights", apply=False)
    assert "1 UPC(s) could not be resolved" in out["warning"]
    assert out["summary"]["keep_unknown"] == 1 and out["summary"]["delete_foreign"] == 1
