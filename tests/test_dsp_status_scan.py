from copyright_alert import dsp_status_scan as scan
from copyright_alert.dsp_status_scan import (
    DELIVERED_MARK,
    DSP_STATUS_COLUMNS,
    NOT_SENT_MARK,
    TIKTOK_COLUMN,
    build_status_updates,
    is_fully_delivered,
)


def test_fully_delivered_ignores_tiktok_column_o():
    row = [""] * 21
    row[14] = ""  # O / TikTok intentionally ignored
    for idx in range(15, 21):  # P:U
        row[idx] = DELIVERED_MARK

    assert is_fully_delivered(row) is True


def test_not_fully_delivered_when_any_non_tiktok_dsp_missing():
    row = [""] * 21
    row[14] = DELIVERED_MARK  # TikTok cannot make row complete
    for idx in range(15, 21):
        row[idx] = DELIVERED_MARK
    row[17] = ""  # R / YouTube still missing

    assert is_fully_delivered(row) is False


def test_build_status_updates_never_writes_tiktok():
    statuses = {name: DELIVERED_MARK for name in DSP_STATUS_COLUMNS}
    statuses["tiktok"] = DELIVERED_MARK

    updates = build_status_updates(12, statuses)
    cells = [cell for cell, _value in updates]

    assert f"{TIKTOK_COLUMN}12" not in cells
    assert cells == ["P12", "Q12", "R12", "S12", "T12", "U12"]


def test_isrc_only_row_resolves_upc_and_then_queries_audiosalad(monkeypatch):
    calls = []

    def fake_query(url, filters, top_n=100, timeout=240):
        calls.append((url, tuple(filters), top_n))
        if url == scan.ISRC_UPC_LOOKUP_URL:
            return {"rows": [{"upc": "640472546318"}]}
        if url == scan.AUDIOSALAD_STATUS_URL:
            assert filters == ["upc=640472546318"]
            return {
                "rows": [
                    {"delivery_target_name": "Apple Music", "delivery_status": "ok"},
                    {"delivery_target_name": "Apple Music (Direct)", "delivery_status": "ok"},
                    {"delivery_target_name": "Spotify (Direct)", "delivery_status": "ok"},
                    {"delivery_target_name": "Meta Audio Library", "delivery_status": "ok"},
                    {"delivery_target_name": "YouTube CID", "delivery_status": "ok"},
                    {"delivery_target_name": "Deezer", "delivery_status": "ok"},
                ]
            }
        raise AssertionError(url)

    monkeypatch.setattr(scan, "_query_aeolus_url", fake_query)
    row = [""] * 21
    row[10] = "BRXXX2400001"  # K / ISRC

    statuses = scan.query_dsp_statuses(row)

    assert statuses["apple"] == DELIVERED_MARK
    assert statuses["spotify"] == DELIVERED_MARK
    assert statuses["facebook"] == DELIVERED_MARK
    assert statuses["youtube"] == DELIVERED_MARK
    assert statuses["deezer"] == DELIVERED_MARK
    assert statuses["soundcloud"] == NOT_SENT_MARK
    assert calls[0] == (scan.ISRC_UPC_LOOKUP_URL, ("isrc=BRXXX2400001",), 5)


def test_isrc_only_row_without_upc_marks_all_not_sent(monkeypatch):
    monkeypatch.setattr(scan, "_query_aeolus_url", lambda *args, **kwargs: {"rows": []})
    row = [""] * 21
    row[10] = "BRXXX2400002"  # K / ISRC

    assert scan.query_dsp_statuses(row) == {name: NOT_SENT_MARK for name in DSP_STATUS_COLUMNS}


def test_run_writes_resolved_upc_before_statuses(monkeypatch):
    row = [""] * 21
    row[10] = "BRXXX2400003"  # K / ISRC
    writes = []

    monkeypatch.setattr(scan, "read_rows_to_process", lambda: ([], [(7, row)], 0))
    monkeypatch.setattr(scan, "lookup_upc_by_isrc", lambda isrc: "640472546318")
    monkeypatch.setattr(
        scan,
        "query_audiosalad_statuses_by_upc",
        lambda upc: {name: NOT_SENT_MARK for name in DSP_STATUS_COLUMNS},
    )
    monkeypatch.setattr(scan, "write_status_updates", lambda updates: writes.extend(updates) or len(updates))

    summary = scan.run(dry_run=False)

    assert writes[0] == ("J7", "640472546318")
    assert ("O7", NOT_SENT_MARK) not in writes
    assert summary["upcs_filled"] == 1
    assert summary["written_cells"] == 7
