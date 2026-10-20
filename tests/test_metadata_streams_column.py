import json

import pytest

from copyright_alert import metadata_notice as mn

S = mn.METADATA_STREAMS_HEADER
OLD_HEADER = ["UPC", "Date Received", "Spotify Notice Type", "Subject", "Status", "Notes",
              "Artist 1", "Artist 2", "Artist 3", "Artist 4", "Artist 5", "Has Infringement Claim?", "Spotify Status"]
NEW_HEADER = OLD_HEADER[:3] + [S] + OLD_HEADER[3:]


def _row(upc, streams=None):
    r = [upc, "2026-07-25 06:03", "Artist/origin metadata misrepresentation", "Some notice", "", "n"]
    if streams is not None:
        r.insert(3, streams)
    return r


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    mn._STREAMS_COLUMN_READY.clear()
    monkeypatch.setattr(mn, "_metadata_tracker_url", lambda region: f"https://sheet/{region}")


def _record_writes(monkeypatch):
    calls = []
    monkeypatch.setattr(mn, "_run_lark_sheets", lambda args, input_text=None: calls.append((args, input_text)) or {})
    return calls


def test_streams_is_the_fourth_header_so_it_lands_in_column_d():
    assert mn.METADATA_CORRECTIONS_HEADERS[3] == S
    assert mn.METADATA_CORRECTIONS_HEADERS[4] == "Subject"


def test_ensure_inserts_column_d_then_writes_the_header(monkeypatch):
    calls = _record_writes(monkeypatch)
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (OLD_HEADER, [], "sid"))
    result = mn._ensure_streams_column("BR")
    assert result["inserted"] is True and result["column"] == "D"
    assert calls[0][0][:1] == ["+dim-insert"] and "D" in calls[0][0] and "--sheet-id" in calls[0][0]
    assert calls[1][0][:1] == ["+csv-put"] and "D1" in calls[1][0] and S in calls[1][0][-1]


def test_ensure_is_a_noop_when_the_column_exists(monkeypatch):
    calls = _record_writes(monkeypatch)
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (NEW_HEADER, [], "sid"))
    assert mn._ensure_streams_column("US")["already_present"] is True and calls == []


def test_ensure_handles_the_real_br_layout_with_an_extra_providers_link_column(monkeypatch):
    _record_writes(monkeypatch)
    br = OLD_HEADER[:12] + ["Providers link", "Spotify Status"]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (br, [], "sid"))
    assert mn._ensure_streams_column("BR")["inserted"] is True


def test_ensure_refuses_an_unexpected_layout_instead_of_shifting_it(monkeypatch):
    calls = _record_writes(monkeypatch)
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (["Foo", "UPC", "Subject"], [], "sid"))
    with pytest.raises(RuntimeError, match="refusing to insert"):
        mn._ensure_streams_column("BR")
    assert calls == []


def test_ensure_resumes_when_a_previous_run_inserted_but_never_wrote_the_header(monkeypatch):
    calls = _record_writes(monkeypatch)
    half = OLD_HEADER[:3] + [""] + OLD_HEADER[3:]
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (half, [], "sid"))
    result = mn._ensure_streams_column("BR")
    assert result["inserted"] is False and len(calls) == 1 and calls[0][0][0] == "+csv-put"   # no 2nd insert


def test_ensure_dry_run_writes_nothing(monkeypatch):
    calls = _record_writes(monkeypatch)
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (OLD_HEADER, [], "sid"))
    assert mn._ensure_streams_column("BR", dry_run=True)["would_insert"] is True and calls == []


def test_append_inserts_the_column_first_then_writes_the_row_with_streams_at_d(monkeypatch):
    calls = _record_writes(monkeypatch)
    monkeypatch.setattr(mn, "_ensure_metadata_corrections_sheet", lambda region: "sid")
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (OLD_HEADER, [], "sid"))
    mn._append_metadata_correction_row({"upc": "111", "date_received": "d", "subject": "s", "ref_id": "r"},
                                       "BR", ["A", "", "", "", ""], streams=4200)
    ops = [c[0][0] for c in calls]
    assert ops == ["+dim-insert", "+csv-put", "+table-put"]          # column exists before the row is written
    payload = json.loads(calls[2][1])["sheets"][0]
    row = payload["data"][0]
    assert row[3] == 4200 and row[4] == "s" and len(row) == len(payload["columns"]) == len(mn.METADATA_CORRECTIONS_HEADERS)


def test_append_defaults_missing_streams_to_na(monkeypatch):
    calls = _record_writes(monkeypatch)
    monkeypatch.setattr(mn, "_ensure_metadata_corrections_sheet", lambda region: "sid")
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (NEW_HEADER, [], "sid"))
    mn._append_metadata_correction_row({"upc": "111"}, "US", None)
    assert json.loads(calls[0][1])["sheets"][0]["data"][0][3] == "N/A"


def test_streams_for_upcs_parses_floats_and_marks_missing_as_na(monkeypatch):
    monkeypatch.setattr(mn, "batch_query_engagement_by_upc",
                        lambda upcs: {"1": {"sptf_30d_str": "1234.0"}, "2": {"sptf_30d_str": None}})
    assert mn._streams_for_upcs(["1", "2", "3", "1", "N/A", ""]) == {"1": 1234, "2": "N/A", "3": "N/A"}


# --- backfill ------------------------------------------------------------------------

def _full_row(upc, streams="", spotify_status=""):
    """A NEW_HEADER-shaped row (14 cells) with Spotify Status in the last column."""
    r = [upc, "2026-07-25 06:03", "Artist/origin metadata misrepresentation", streams, "Some notice", "", "n",
         "", "", "", "", "", "", spotify_status]
    assert len(r) == len(NEW_HEADER)
    return r


def test_backfill_fills_only_blank_cells_online_rows_get_numbers_the_rest_na(monkeypatch):
    rows = [_full_row("111", streams="999", spotify_status="Online"),    # row 2: scan-time value -> untouched
            _full_row("222", spotify_status="Online"),                    # row 3: Online -> today's number
            _full_row("333", spotify_status="Online"),                    # row 4: protected -> never written
            _full_row("444", spotify_status="Online"),                    # row 5: Online, no Aeolus data -> N/A
            _full_row("555", spotify_status="Offline"),                   # row 6: taken down -> N/A (not a misleading 0)
            _full_row("666", spotify_status="")]                          # row 7: status never backfilled -> N/A
    calls = _record_writes(monkeypatch)
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (NEW_HEADER, rows, "sid"))
    monkeypatch.setattr(mn, "_METADATA_PROTECTED_ROWS", {"BR": {4}})
    queried = []
    monkeypatch.setattr(mn, "batch_query_engagement_by_upc",
                        lambda upcs: queried.append(sorted(upcs)) or {"222": {"sptf_30d_str": "52000"}})

    summary = mn.backfill_metadata_streams("BR", dry_run=False)

    assert queried == [["222", "444"]]                      # only Online, unprotected, blank rows are queried
    assert summary["rows_to_fill"] == 4 and summary["online_rows_filled_with_streams"] == 2
    assert summary["not_online_set_to_na"] == 2 and summary["protected_rows_skipped"] == 1
    csv_calls = [c[0] for c in calls if c[0][0] == "+csv-put"]
    written = [(a[a.index("--start-cell") + 1], a[-1].strip().split("\n")) for a in csv_calls]
    # row 3 alone (row 4 is protected), then rows 5-7 as one contiguous block
    assert written == [("D3", ["52000"]), ("D5", ["N/A", "N/A", "N/A"])]


def test_backfill_dry_run_on_a_tab_without_the_column_writes_nothing_and_treats_every_row_as_blank(monkeypatch):
    rows = [_row("111"), _row("222")]
    for r in rows:
        r.insert(3, "Some Subject")   # old layout: index 3 is Subject, NOT streams
    calls = _record_writes(monkeypatch)
    monkeypatch.setattr(mn, "_read_metadata_tracker_rows", lambda region: (OLD_HEADER, rows, "sid"))
    monkeypatch.setattr(mn, "batch_query_engagement_by_upc", lambda upcs: {})
    summary = mn.backfill_metadata_streams("US", dry_run=True)
    assert calls == [] and summary["rows_to_fill"] == 2 and summary["column"]["would_insert"] is True
    assert summary["not_online_set_to_na"] == 2          # no 'Spotify Status' values -> N/A, not zeros
