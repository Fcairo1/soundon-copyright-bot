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


def test_backfill_dry_run_fills_blank_and_new_status_does_not_write(monkeypatch):
    rows = [_row("111", status="New"), _row("222", status=""), _row("333", status="⚖️ Disputing")]
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
    assert summary["status_blank_filled"] == 2  # "New" + the truly-blank row
    assert summary["headers_already_present"] is False
    assert summary["sample"][0]["status"] == "🔍 Investigating"  # "New" -> filled
    assert summary["sample"][1]["status"] == "🔍 Investigating"  # blank -> filled
    assert summary["sample"][2]["status"] == "⚖️ Disputing"  # real ops status -> untouched


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
    assert summary["status_rows_written"] == 2  # "New" and the blank row both get filled

    start_cells = [args[args.index("--start-cell") + 1] for args in calls]
    assert "G1" in start_cells  # artist headers
    assert "G2" in start_cells  # artist data block
    assert "E2" in start_cells  # status column, only the blank row's worth of writes needed


def test_backfill_skips_status_write_when_nothing_blank(monkeypatch):
    rows = [_row("111", status="🔴 Confirm Takedown")]
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


def test_new_row_has_blank_placeholders_for_claim_and_spotify_status_columns(monkeypatch):
    # A brand-new notice must never pay for a live claims-tab read or a live
    # Spotify check inline — those two new columns start blank and are filled
    # in by the next backfill run, same as Artist 1-5 already work.
    captured = {}
    monkeypatch.setattr(mn, "_ensure_metadata_corrections_sheet", lambda region: "sid")

    def fake_run_lark_sheets(args, input_text=None):
        if "+table-put" in args:
            import json
            captured["payload"] = json.loads(input_text)
        return {}
    monkeypatch.setattr(mn, "_run_lark_sheets", fake_run_lark_sheets)

    fields = {"upc": "111", "date_received": "2026-07-27", "subject": "s", "ref_id": "ref:x"}
    mn._append_metadata_correction_row(fields, "BR")

    row = captured["payload"]["sheets"][0]["data"][0]
    assert len(row) == len(mn.METADATA_CORRECTIONS_HEADERS)
    assert row[-2:] == ["", ""]


# ── Has Infringement Claim? / Spotify Status ─────────────────────────────────

def test_spotify_id_and_kind_from_uri_parses_track_album_and_url_forms():
    assert mn._spotify_id_and_kind_from_uri("spotify:track:2WCOfPUgEYmzppOlyR08bd") == \
        ("2WCOfPUgEYmzppOlyR08bd", "track")
    assert mn._spotify_id_and_kind_from_uri("spotify:album:1a2b3c4d5e") == ("1a2b3c4d5e", "album")
    assert mn._spotify_id_and_kind_from_uri("https://open.spotify.com/track/2WCOfPUgEYmzppOlyR08bd?si=x") == \
        ("2WCOfPUgEYmzppOlyR08bd", "track")
    assert mn._spotify_id_and_kind_from_uri("not a uri") == (None, None)
    assert mn._spotify_id_and_kind_from_uri("") == (None, None)
    assert mn._spotify_id_and_kind_from_uri(None) == (None, None)


def test_check_spotify_status_unknown_for_unparseable_uri_no_network_call(monkeypatch):
    monkeypatch.setattr(mn.urllib.request, "urlopen",
                         lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call network")))
    assert mn.check_spotify_status("garbage") == "Unknown"
    assert mn.check_spotify_status("") == "Unknown"


def test_check_spotify_status_maps_http_200_to_online(monkeypatch):
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(mn.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert mn.check_spotify_status("spotify:track:abc123") == "Online"


def test_check_spotify_status_maps_http_404_to_offline_not_error(monkeypatch):
    def raise_404(*a, **k):
        raise mn.urllib.error.HTTPError("url", 404, "Not Found", {}, None)
    monkeypatch.setattr(mn.urllib.request, "urlopen", raise_404)
    assert mn.check_spotify_status("spotify:track:abc123") == "Offline"


def test_check_spotify_status_never_reports_offline_on_transient_failure(monkeypatch):
    def raise_timeout(*a, **k):
        raise TimeoutError("network slow")
    monkeypatch.setattr(mn.urllib.request, "urlopen", raise_timeout)
    assert mn.check_spotify_status("spotify:track:abc123") == "Unknown"

    def raise_500(*a, **k):
        raise mn.urllib.error.HTTPError("url", 500, "Server Error", {}, None)
    monkeypatch.setattr(mn.urllib.request, "urlopen", raise_500)
    assert mn.check_spotify_status("spotify:track:abc123") == "Unknown"


def test_spotify_uri_for_upc_picks_most_recently_seen_match():
    notices = {
        "ref:old": {"first_seen": "2026-01-01T00:00:00Z", "fields": {"upc": "111", "spotify_uri": "spotify:track:old"}},
        "ref:new": {"first_seen": "2026-06-01T00:00:00Z", "fields": {"upc": "111", "spotify_uri": "spotify:track:new"}},
        "ref:other": {"first_seen": "2026-09-01T00:00:00Z", "fields": {"upc": "222", "spotify_uri": "spotify:track:z"}},
    }
    assert mn._spotify_uri_for_upc("111", notices) == "spotify:track:new"


def test_spotify_uri_for_upc_empty_when_no_match_or_placeholder_value():
    notices = {
        "ref:a": {"first_seen": "2026-01-01T00:00:00Z", "fields": {"upc": "111", "spotify_uri": "N/A"}},
    }
    assert mn._spotify_uri_for_upc("111", notices) == ""
    assert mn._spotify_uri_for_upc("999", notices) == ""


def test_read_claims_upcs_reads_once_and_parses_upc_column(monkeypatch):
    calls = []

    def fake_run_lark_sheets(args, **k):
        calls.append(args)
        csv_text = "UPC,ISRC,Title\n111,ISRC1,T1\n222,ISRC2,T2\n111,ISRC3,T3\n"
        return {"data": {"annotated_csv": csv_text}}

    monkeypatch.setattr(mn, "_claims_tracker_location", lambda region: ("https://tracker", "sid"))
    monkeypatch.setattr(mn, "_run_lark_sheets", fake_run_lark_sheets)

    upcs = mn._read_claims_upcs("BR")
    assert upcs == {"111", "222"}
    assert len(calls) == 1  # exactly one read, not one per UPC


HEADER_WITH_NEW_COLS = HEADER + mn.METADATA_ARTIST_HEADERS + mn.METADATA_NEW_HEADERS


def _row_full(upc):
    return _row(upc) + ["", "", "", "", ""] + ["", ""]


def test_backfill_claim_and_spotify_dry_run_flags_match_and_no_match(monkeypatch):
    rows = [_row_full("111"), _row_full("222")]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (HEADER_WITH_NEW_COLS, rows, "sid"))
    monkeypatch.setattr(mn, "_read_claims_upcs", lambda region: {"111"})
    monkeypatch.setattr(mn, "_load_state", lambda: {"notices": {}})
    monkeypatch.setattr(mn, "check_spotify_status", lambda uri: "Online")
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))

    summary = mn.backfill_metadata_claim_and_spotify_status("BR", dry_run=True)
    assert summary["has_claim_yes"] == 1
    assert summary["sample"][0]["has_claim"] == "Yes"   # UPC 111 is in the claims set
    assert summary["sample"][1]["has_claim"] == ""       # UPC 222 is not
    assert summary["spotify_unknown"] == 2  # no notices on file -> no resolvable URI for either row


def test_backfill_uses_resolved_spotify_uri_when_available(monkeypatch):
    rows = [_row_full("111")]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (HEADER_WITH_NEW_COLS, rows, "sid"))
    monkeypatch.setattr(mn, "_read_claims_upcs", lambda region: set())
    monkeypatch.setattr(mn, "_load_state", lambda: {"notices": {
        "ref:1": {"first_seen": "2026-01-01T00:00:00Z", "fields": {"upc": "111", "spotify_uri": "spotify:track:abc"}},
    }})
    seen_uris = []
    monkeypatch.setattr(mn, "check_spotify_status", lambda uri: seen_uris.append(uri) or "Offline")
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))

    summary = mn.backfill_metadata_claim_and_spotify_status("BR", dry_run=True)
    assert seen_uris == ["spotify:track:abc"]
    assert summary["sample"][0]["spotify_status"] == "Offline"


def test_backfill_protected_rows_never_computed_or_written(monkeypatch):
    # Use a small synthetic protected-row set instead of the real 235/1787 so
    # the test doesn't need hundreds of filler rows — the mechanism under
    # test is "skip this sheet row entirely", not the specific row numbers.
    monkeypatch.setattr(mn, "_METADATA_PROTECTED_ROWS", {"BR": {2}})  # first data row = sheet row 2
    rows = [_row_full("111"), _row_full("222")]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (HEADER_WITH_NEW_COLS, rows, "sid"))
    monkeypatch.setattr(mn, "_read_claims_upcs", lambda region: {"111", "222"})
    monkeypatch.setattr(mn, "_load_state", lambda: {"notices": {}})
    monkeypatch.setattr(mn, "check_spotify_status", lambda uri: "Online")

    written_ranges = []

    def fake_run_lark_sheets(args, **k):
        if "--start-cell" in args:
            written_ranges.append(args[args.index("--start-cell") + 1])
        return {}
    monkeypatch.setattr(mn, "_run_lark_sheets", fake_run_lark_sheets)

    summary = mn.backfill_metadata_claim_and_spotify_status("BR", dry_run=False)
    assert summary["protected_rows_skipped"] == 1
    assert summary["sample"][0]["has_claim"] is None  # row 2 (the protected one) never computed
    assert summary["sample"][1]["has_claim"] == "Yes"
    # The protected row (sheet row 2) must never appear as a write start-cell,
    # and the surviving row (sheet row 3) must be written on its own segment.
    claim_col = mn._column_letter(HEADER_WITH_NEW_COLS.index(mn.METADATA_CLAIM_HEADER))
    assert f"{claim_col}2" not in written_ranges
    assert f"{claim_col}3" in written_ranges


def test_backfill_real_run_writes_headers_when_not_already_present(monkeypatch):
    header_without_new_cols = HEADER + mn.METADATA_ARTIST_HEADERS
    rows = [_row(upc="111") + ["", "", "", "", ""]]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (header_without_new_cols, rows, "sid"))
    monkeypatch.setattr(mn, "_read_claims_upcs", lambda region: set())
    monkeypatch.setattr(mn, "_load_state", lambda: {"notices": {}})
    monkeypatch.setattr(mn, "check_spotify_status", lambda uri: "Unknown")

    calls = []
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda args, **k: calls.append(args) or {})

    summary = mn.backfill_metadata_claim_and_spotify_status("BR", dry_run=False)
    assert summary["headers_written"] is True
    start_cells = [args[args.index("--start-cell") + 1] for args in calls]
    assert "L1" in start_cells  # 2 new headers appended right after the 11 existing columns


def test_backfill_skips_header_write_when_new_headers_already_present(monkeypatch):
    rows = [_row_full("111")]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (HEADER_WITH_NEW_COLS, rows, "sid"))
    monkeypatch.setattr(mn, "_read_claims_upcs", lambda region: set())
    monkeypatch.setattr(mn, "_load_state", lambda: {"notices": {}})
    monkeypatch.setattr(mn, "check_spotify_status", lambda uri: "Unknown")

    calls = []
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda args, **k: calls.append(args) or {})

    summary = mn.backfill_metadata_claim_and_spotify_status("BR", dry_run=False)
    assert summary["headers_written"] is False
    start_cells = [args[args.index("--start-cell") + 1] for args in calls]
    assert "L1" not in start_cells
