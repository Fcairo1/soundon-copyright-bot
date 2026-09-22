from datetime import date

from copyright_alert import handle_callback as hc
from copyright_alert import marketshare as ms
from copyright_alert import marketshare_tracker as mt

HEADER = ["UPC", "Status", "Date Received", "Email Status", "Retracted",
          ms.MARKETSHARE_AT_CLAIM_HEADER, ms.MARKETSHARE_CURRENT_HEADER]


def _values(*rows):
    return [HEADER, *rows]


def _patch_common(monkeypatch, values):
    monkeypatch.setattr(hc, "_tracker_config", lambda region=None: ("https://sheet", "sid"))
    monkeypatch.setattr(hc, "read_sheet_values", lambda region=None: values)
    monkeypatch.setattr(mt, "ensure_columns", lambda region: {"created": []})


def test_ensure_columns_appends_missing_headers(monkeypatch):
    writes = []
    monkeypatch.setattr(hc, "_tracker_config", lambda region=None: ("https://sheet", "sid"))
    monkeypatch.setattr(hc, "read_sheet_values", lambda region=None: [["UPC", "Status"]])
    monkeypatch.setattr(hc, "_sheet_api", lambda *a, **k: writes.append(a) or {"code": 0})
    result = mt.ensure_columns("BR")
    assert result["created"] == [ms.MARKETSHARE_AT_CLAIM_HEADER, ms.MARKETSHARE_CURRENT_HEADER]
    assert len(writes) == 2


def test_ensure_columns_handles_padded_wide_header_row(monkeypatch):
    """Regression for the 2026-09-22 incident: read_sheet_values pads every
    row to a fixed width (78 cols), so a header row with real headers only
    through column AF (32) but padded with 46 blank trailing cells must NOT
    be treated as "78 columns in use" — that overwrote the live "Retracted"
    header (AA) on the real BR tracker."""
    headers_row = ["UPC", "Status"] + ["H%d" % i for i in range(30)] + [""] * 46  # 32 real + 46 padding = 78
    assert len(headers_row) == 78
    monkeypatch.setattr(hc, "_tracker_config", lambda region=None: ("https://sheet", "sid"))
    monkeypatch.setattr(hc, "read_sheet_values", lambda region=None: [headers_row])
    writes = []
    monkeypatch.setattr(hc, "_sheet_api", lambda *a, **k: writes.append(a[3]) or {"code": 0})
    result = mt.ensure_columns("BR")
    assert result["created"] == [ms.MARKETSHARE_AT_CLAIM_HEADER, ms.MARKETSHARE_CURRENT_HEADER]
    # must land right after the 32 real headers (index 32 -> "AG"), not at
    # the padded width (index 78 -> "CA")
    assert writes == ["AG1", "AH1"]


def test_ensure_columns_noop_when_present(monkeypatch):
    monkeypatch.setattr(hc, "_tracker_config", lambda region=None: ("https://sheet", "sid"))
    monkeypatch.setattr(hc, "read_sheet_values", lambda region=None: [HEADER])
    monkeypatch.setattr(hc, "_sheet_api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not write")))
    assert mt.ensure_columns("BR")["created"] == []


def test_backfill_uses_date_received_as_end_date(monkeypatch):
    values = _values(["638022638262", "🔴 Confirm Takedown", "2026-09-01", "", "", "", ""])
    _patch_common(monkeypatch, values)
    calls = []
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upc", lambda upc, end: calls.append((upc, end)) or 12.5)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upcs", lambda upcs, end: {})
    writes = []
    import copyright_alert.lark_auth as lark_auth
    monkeypatch.setattr(lark_auth, "sheet_values_batch_update", lambda url, ranges: writes.append(ranges))
    result = mt.run_daily_sync("BR")
    assert calls == [("638022638262", date(2026, 9, 1))]
    assert result["at_claim_backfilled"] == 1
    assert writes[0][0]["values"] == [[12.5]]


def test_skips_rows_without_upc_and_without_date(monkeypatch):
    values = _values(["", "🔴 Confirm Takedown", "2026-09-01", "", "", "", ""],
                     ["638022638262", "🔴 Confirm Takedown", "", "", "", "", ""])
    _patch_common(monkeypatch, values)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upc", lambda upc, end: 1.0)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upcs", lambda upcs, end: {})
    result = mt.run_daily_sync("BR", dry_run=True)
    assert result["skipped_no_date_received"] == 1
    assert result["at_claim_backfilled"] == 0


def test_already_has_at_claim_is_not_recomputed(monkeypatch):
    values = _values(["638022638262", "🔴 Confirm Takedown", "2026-09-01", "", "", "5.0", ""])
    _patch_common(monkeypatch, values)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upc", lambda upc, end: (_ for _ in ()).throw(AssertionError("should not recompute")))
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upcs", lambda upcs, end: {})
    result = mt.run_daily_sync("BR", dry_run=True)
    assert result["at_claim_backfilled"] == 0


def test_open_claim_refreshes_current_via_batch(monkeypatch):
    values = _values(
        ["638022638262", "🔍 Investigating", "2026-09-01", "", "", "5.0", ""],   # open -> refresh
        ["999999999999", "🔴 Confirm Takedown", "2026-09-01", "", "", "5.0", ""],  # lost -> not refreshed
    )
    _patch_common(monkeypatch, values)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upc", lambda upc, end: 1.0)
    batch_calls = []

    def fake_batch(upcs, end):
        batch_calls.append((list(upcs), end))
        return {"638022638262": 8.25}

    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upcs", fake_batch)
    result = mt.run_daily_sync("BR", dry_run=True, today=date(2026, 9, 22))
    assert batch_calls == [(["638022638262"], date(2026, 9, 22))]
    assert result["current_refreshed"] == 1


def test_backfill_respects_per_run_cap_and_calls_batch_resolve(monkeypatch):
    rows = [[f"UPC{i}", "🔴 Confirm Takedown", "2026-09-01", "", "", "", ""] for i in range(mt.MAX_BACKFILL_PER_RUN + 5)]
    values = _values(*rows)
    _patch_common(monkeypatch, values)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upc", lambda upc, end: 1.0)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upcs", lambda upcs, end: {})
    batch_calls = []
    monkeypatch.setattr(ms, "batch_resolve_upcs", lambda upcs: batch_calls.append(list(upcs)))
    result = mt.run_daily_sync("BR", dry_run=True)
    assert result["at_claim_backfilled"] == mt.MAX_BACKFILL_PER_RUN
    assert result["at_claim_pending_next_run"] == 5
    assert len(batch_calls[0]) == mt.MAX_BACKFILL_PER_RUN


def test_current_refresh_respects_per_run_cap(monkeypatch):
    rows = [[f"UPC{i}", "🔍 Investigating", "2026-09-01", "", "", "5.0", ""] for i in range(mt.MAX_CURRENT_REFRESH_PER_RUN + 4)]
    values = _values(*rows)
    _patch_common(monkeypatch, values)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upc", lambda upc, end: 1.0)
    batch_calls = []

    def fake_batch(upcs, end):
        batch_calls.append(list(upcs))
        return {u: 1.0 for u in upcs}

    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upcs", fake_batch)
    result = mt.run_daily_sync("BR", dry_run=True, today=date(2026, 9, 22))
    assert result["current_refreshed"] == mt.MAX_CURRENT_REFRESH_PER_RUN
    assert result["current_pending_next_run"] == 4
    assert len(batch_calls[0]) == mt.MAX_CURRENT_REFRESH_PER_RUN


def test_dry_run_writes_nothing(monkeypatch):
    values = _values(["638022638262", "🔴 Confirm Takedown", "2026-09-01", "", "", "", ""])
    _patch_common(monkeypatch, values)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upc", lambda upc, end: 1.0)
    monkeypatch.setattr(ms, "get_marketshare_ppm_for_upcs", lambda upcs, end: {})
    import copyright_alert.lark_auth as lark_auth
    monkeypatch.setattr(lark_auth, "sheet_values_batch_update", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))
    result = mt.run_daily_sync("BR", dry_run=True)
    assert result["dry_run"] is True and result["write_count"] == 1


def test_error_when_columns_missing_after_ensure(monkeypatch):
    monkeypatch.setattr(hc, "_tracker_config", lambda region=None: ("https://sheet", "sid"))
    monkeypatch.setattr(hc, "read_sheet_values", lambda region=None: [["UPC", "Status"]])
    monkeypatch.setattr(mt, "ensure_columns", lambda region: {"created": []})
    result = mt.run_daily_sync("BR", dry_run=True)
    assert "error" in result
