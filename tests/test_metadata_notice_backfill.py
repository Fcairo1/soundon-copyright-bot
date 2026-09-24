from copyright_alert import metadata_notice as mn

HEADER = ["UPC", "Date Received", "Spotify Notice Type", "Subject", "Status", "Notes"]


def _row(upc, status=""):
    return [upc, "2026-07-25 06:03", "Artist/origin metadata misrepresentation", "Some notice", status, "Notes text"]


def test_artist_columns_splits_up_to_four_then_joins_the_rest():
    assert mn.artist_columns_from_display_artist('["Anna Luz","DJ Lano SP"]') == \
        ["Anna Luz", "DJ Lano SP", "", "", ""]


def test_artist_columns_handles_none_and_blank():
    assert mn.artist_columns_from_display_artist(None) == ["", "", "", "", ""]
    assert mn.artist_columns_from_display_artist("") == ["", "", "", "", ""]


def test_artist_columns_joins_fifth_and_beyond_into_artist_five():
    raw = '["A1","A2","A3","A4","A5","A6"]'
    assert mn.artist_columns_from_display_artist(raw) == ["A1", "A2", "A3", "A4", "A5, A6"]


def test_artist_columns_five_exactly_no_join_needed():
    raw = '["A1","A2","A3","A4","A5"]'
    assert mn.artist_columns_from_display_artist(raw) == ["A1", "A2", "A3", "A4", "A5"]


def test_backfill_resolved_count_is_per_unique_upc_not_per_row(monkeypatch):
    # Real production bug: a UPC appearing on 2 rows must not make
    # upcs_resolved_via_aeolus (and thus upcs_not_found) double-count it.
    rows = [_row("111", status="New"), _row("111", status=""), _row("222", status="")]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (HEADER, rows, "sid"))
    monkeypatch.setattr(mn, "batch_query_aeolus_by_upc", lambda upcs: {
        "111": {"display_artist": '["Artist A"]'},
    })
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))

    summary = mn.backfill_metadata_artists_and_status("BR", dry_run=True)
    assert summary["unique_upcs"] == 2
    assert summary["upcs_resolved_via_aeolus"] == 1
    assert summary["upcs_not_found"] == 1


def test_backfill_dry_run_fills_only_blank_status_and_does_not_write(monkeypatch):
    rows = [_row("111", status="New"), _row("222", status=""), _row("333", status="")]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (HEADER, rows, "sid"))
    monkeypatch.setattr(mn, "batch_query_aeolus_by_upc", lambda upcs: {
        "111": {"display_artist": '["Artist A"]'},
        "222": {"display_artist": '["Artist B","Artist C"]'},
    })
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))

    summary = mn.backfill_metadata_artists_and_status("BR", dry_run=True)
    assert summary["rows"] == 3
    assert summary["unique_upcs"] == 3
    assert summary["upcs_resolved_via_aeolus"] == 2
    assert summary["upcs_not_found"] == 1
    assert summary["status_blank_filled"] == 2
    assert summary["headers_already_present"] is False
    assert summary["sample"][0]["status"] == "New"
    assert summary["sample"][1]["status"] == "🔍 Investigating"


def test_backfill_real_run_writes_headers_artist_block_and_status_block(monkeypatch):
    rows = [_row("111", status="New"), _row("222", status="")]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (HEADER, rows, "sid"))
    monkeypatch.setattr(mn, "batch_query_aeolus_by_upc", lambda upcs: {
        "111": {"display_artist": '["Artist A"]'},
        "222": {"display_artist": '["Artist B","Artist C"]'},
    })
    calls = []
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda args, **k: calls.append(args) or {})

    summary = mn.backfill_metadata_artists_and_status("BR", dry_run=False)
    assert summary["headers_written"] is True
    assert summary["status_rows_written"] == 1

    start_cells = [args[args.index("--start-cell") + 1] for args in calls]
    assert "G1" in start_cells  # artist headers
    assert "G2" in start_cells  # artist data block
    assert "E2" in start_cells  # status column, only the blank row's worth of writes needed


def test_backfill_skips_status_write_when_nothing_blank(monkeypatch):
    rows = [_row("111", status="New")]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (HEADER, rows, "sid"))
    monkeypatch.setattr(mn, "batch_query_aeolus_by_upc", lambda upcs: {})
    calls = []
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda args, **k: calls.append(args) or {})

    summary = mn.backfill_metadata_artists_and_status("BR", dry_run=False)
    assert summary["status_rows_written"] == 0
    start_cells = [args[args.index("--start-cell") + 1] for args in calls]
    assert "E2" not in start_cells


def test_backfill_skips_header_write_when_already_present(monkeypatch):
    header = HEADER + mn.METADATA_ARTIST_HEADERS
    rows = [_row("111", status="New") + ["", "", "", "", ""]]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (header, rows, "sid"))
    monkeypatch.setattr(mn, "batch_query_aeolus_by_upc", lambda upcs: {})
    calls = []
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda args, **k: calls.append(args) or {})

    summary = mn.backfill_metadata_artists_and_status("BR", dry_run=False)
    assert summary["headers_written"] is False
    start_cells = [args[args.index("--start-cell") + 1] for args in calls]
    assert "G1" not in start_cells


def test_new_row_default_status_and_artist_columns(monkeypatch):
    captured = {}

    def fake_ensure(region):
        return "sid"

    def fake_run_lark_sheets(args, input_text=None):
        if "+table-put" in args:
            import json
            captured["payload"] = json.loads(input_text)
        return {}

    monkeypatch.setattr(mn, "_ensure_metadata_corrections_sheet", fake_ensure)
    monkeypatch.setattr(mn, "_run_lark_sheets", fake_run_lark_sheets)

    fields = {"upc": "5063971200997", "date_received": "2026-07-27", "subject": "Some subject",
              "ref_id": "ref:x"}
    mn._append_metadata_correction_row(fields, "BR", ["Anna Luz", "DJ Lano SP", "", "", ""])

    row = captured["payload"]["sheets"][0]["data"][0]
    assert row[4] == "🔍 Investigating"
    assert row[6:8] == ["Anna Luz", "DJ Lano SP"]
