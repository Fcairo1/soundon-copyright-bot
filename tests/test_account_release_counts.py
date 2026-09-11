from datetime import datetime
from zoneinfo import ZoneInfo

from copyright_alert import account_release_counts as arc


def test_current_quarter_q3_2026_boundaries():
    quarter = arc.current_quarter(datetime(2026, 9, 8, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo")))

    assert quarter == {
        "label": "Q3 2026",
        "start": "2026-07-01",
        "end_exclusive": "2026-10-01",
    }


def test_current_quarter_rolls_across_all_quarters_in_brazil_time():
    examples = [
        (datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("America/Sao_Paulo")), "Q1 2027", "2027-01-01", "2027-04-01"),
        (datetime(2027, 4, 15, 9, 0, tzinfo=ZoneInfo("America/Sao_Paulo")), "Q2 2027", "2027-04-01", "2027-07-01"),
        (datetime(2027, 7, 31, 23, 59, tzinfo=ZoneInfo("America/Sao_Paulo")), "Q3 2027", "2027-07-01", "2027-10-01"),
        (datetime(2027, 12, 31, 23, 59, tzinfo=ZoneInfo("America/Sao_Paulo")), "Q4 2027", "2027-10-01", "2028-01-01"),
    ]

    for now, label, start, end_exclusive in examples:
        assert arc.current_quarter(now) == {"label": label, "start": start, "end_exclusive": end_exclusive}


def test_inspect_account_release_sheet_finds_existing_and_quarter_column():
    inspected = arc.inspect_account_release_sheet([
        ["uid", "user_name", "last_updated", "total_releases", "Q2 2026", "Q3 2026"],
        ["111", "One", "time", "10", "4", "1"],
        ["", "", "", "", "", ""],
        ["222", "Two", "time", "2", "", "2"],
    ], "Q3 2026")

    assert inspected == {
        "headers": ["uid", "user_name", "last_updated", "total_releases", "Q2 2026", "Q3 2026"],
        "existing": {"111": 2, "222": 4},
        "first_empty_row": 3,
        "quarter_index": 5,
    }


def test_plan_sheet_updates_only_current_quarter_for_existing_and_total_for_new():
    plan = arc.plan_sheet_updates(
        [
            {"uid": "111", "user_name": "One", "current_quarter_releases": 7, "quarter_label": "Q3 2026"},
            {"uid": "333", "user_name": "Three", "total_releases": 12, "current_quarter_releases": 3, "quarter_label": "Q3 2026"},
        ],
        [["uid", "user_name", "last_updated", "total_releases", "Q2 2026"], ["111", "One", "old", "99", "8"]],
        "abc123",
        quarter_label="Q3 2026",
        now=datetime(2026, 9, 8, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo")),
    )

    assert plan["quarter_column_created"] is True
    assert plan["updates"] == [
        {"range": "abc123!F1:F1", "values": [["Q3 2026"]]},
        {"range": "abc123!C2:C2", "values": [["2026-09-08 12:00:00 BRT"]]},
        {"range": "abc123!F2:F2", "values": [["7"]]},
        {"range": "abc123!A3:A3", "values": [["333"]]},
        {"range": "abc123!B3:B3", "values": [["Three"]]},
        {"range": "abc123!C3:C3", "values": [["2026-09-08 12:00:00 BRT"]]},
        {"range": "abc123!D3:D3", "values": [["12"]]},
        {"range": "abc123!F3:F3", "values": [["3"]]},
    ]


def test_query_account_release_counts_sql(monkeypatch):
    captured = {}

    def fake_run(sql, timeout=240):
        captured["sql"] = sql
        return {
            "code": "aeolus/ok",
            "rows": [
                {
                    "uid": 123,
                    "user_name": "Example Account",
                    "total_releases": 10,
                    "current_quarter_releases": 3,
                }
            ],
            "totalRows": 1,
        }

    monkeypatch.setattr(arc, "_run_song_dimension_sql", fake_run)

    row = arc.query_account_release_counts(
        "123",
        quarter={"label": "Q3 2026", "start": "2026-07-01", "end_exclusive": "2026-10-01"},
        p_date="2026-09-08",
    )

    assert row == {
        "uid": "123",
        "user_name": "Example Account",
        "total_releases": 10,
        "current_quarter_releases": 3,
        "quarter_label": "Q3 2026",
    }
    assert f"FROM {arc.SONG_DIMENSION_TABLE}" in captured["sql"]
    assert "COUNT(DISTINCT album_id) AS total_releases" in captured["sql"]
    assert "status_desc != 'REJECT'" in captured["sql"]
    assert "`source_type[dim_user]` IN (5, 6)" in captured["sql"]
    assert "region = 'BR'" in captured["sql"]
    assert "toDate(release_time) >= toDate('2026-07-01')" in captured["sql"]
    assert "toDate(release_time) < toDate('2026-10-01')" in captured["sql"]
    assert "p_date = '2026-09-08'" in captured["sql"]
