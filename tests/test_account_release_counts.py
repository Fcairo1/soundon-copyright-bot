from datetime import datetime
from zoneinfo import ZoneInfo

from copyright_alert import account_release_counts as arc


def test_current_quarter_q3_2026_boundaries():
    quarter = arc.current_quarter(datetime(2026, 9, 8, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo")))

    assert quarter == {
        "label": "2026-Q3",
        "start": "2026-07-01",
        "end_exclusive": "2026-10-01",
    }


def test_current_quarter_rolls_across_all_quarters_in_brazil_time():
    examples = [
        (datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("America/Sao_Paulo")), "2027-Q1", "2027-01-01", "2027-04-01"),
        (datetime(2027, 4, 15, 9, 0, tzinfo=ZoneInfo("America/Sao_Paulo")), "2027-Q2", "2027-04-01", "2027-07-01"),
        (datetime(2027, 7, 31, 23, 59, tzinfo=ZoneInfo("America/Sao_Paulo")), "2027-Q3", "2027-07-01", "2027-10-01"),
        (datetime(2027, 12, 31, 23, 59, tzinfo=ZoneInfo("America/Sao_Paulo")), "2027-Q4", "2027-10-01", "2028-01-01"),
    ]

    for now, label, start, end_exclusive in examples:
        assert arc.current_quarter(now) == {"label": label, "start": start, "end_exclusive": end_exclusive}


def test_build_partial_update_ranges_skips_missing_values():
    ranges = arc.build_partial_update_ranges(
        "abc123",
        7,
        {
            "uid": "123",
            "user_name": "Example",
            "releases_this_quarter": 4,
            "quarter_label": "2026-Q3",
            "last_updated": "2026-09-08 12:00:00 BRT",
        },
    )

    assert ranges == [
        {"range": "abc123!A7:A7", "values": [["123"]]},
        {"range": "abc123!B7:B7", "values": [["Example"]]},
        {"range": "abc123!D7:D7", "values": [["4"]]},
        {"range": "abc123!E7:E7", "values": [["2026-Q3"]]},
        {"range": "abc123!F7:F7", "values": [["2026-09-08 12:00:00 BRT"]]},
    ]


def test_inspect_release_count_rows_finds_existing_and_first_empty_after_header():
    inspected = arc.inspect_release_count_rows([
        arc.COLUMNS,
        ["111", "One", "1", "1", "2026-Q3", "time"],
        ["", "", "", "", "", ""],
        ["222", "Two", "2", "2", "2026-Q3", "time"],
    ])

    assert inspected == {"existing": {"111": 2, "222": 4}, "first_empty_row": 3}


def test_query_account_release_counts_sql(monkeypatch):
    captured = {}

    def fake_run(sql, timeout=180, dataset_id=None):
        captured["sql"] = sql
        captured["dataset_id"] = dataset_id
        return {
            "code": "aeolus/ok",
            "rows": [
                {
                    "uid": 123,
                    "user_name": "Example Account",
                    "total_releases": 10,
                    "releases_this_quarter": 3,
                }
            ],
            "totalRows": 1,
        }

    monkeypatch.setattr(arc.ra, "_run_aeolus_sql", fake_run)

    row = arc.query_account_release_counts(
        "123",
        quarter={"label": "2026-Q3", "start": "2026-07-01", "end_exclusive": "2026-10-01"},
    )

    assert row == {
        "uid": "123",
        "user_name": "Example Account",
        "total_releases": 10,
        "releases_this_quarter": 3,
        "quarter_label": "2026-Q3",
    }
    assert "GROUP BY `[user_id]`" in captured["sql"]
    assert "COUNT(DISTINCT `[album_id]`) AS total_releases" in captured["sql"]
    assert "[album_status]` = 2" in captured["sql"]
    assert "[release_time_format]` >= '2026-07-01'" in captured["sql"]
    assert "[release_time_format]` < '2026-10-01'" in captured["sql"]
    assert "[p_date]` >= '2020-01-01'" in captured["sql"]
