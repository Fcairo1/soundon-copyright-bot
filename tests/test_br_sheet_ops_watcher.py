from copyright_alert import br_sheet_ops_watcher as watcher


def test_collect_br_rows_needing_ops_filters_on_column_c_and_empty_column_o():
    values = [
        ["header"] * 15,
        ["subheader"] * 15,
        ["2026-08-27", "Artist A", "BR", "", "ISRC1", "", "https://video/1", "", "$10", "Reason 1", "Exclusive", "", "", "Need review", ""],
        ["2026-08-27", "Artist B", "US", "", "ISRC2", "", "https://video/2", "", "$20", "Reason 2", "Exclusive", "", "", "Need review", ""],
        ["2026-08-27", "Artist C", "BR", "", "ISRC3", "", "https://video/3", "", "$30", "Reason 3", "Exclusive", "", "", "Need review", "2026-08-28"],
    ]

    rows = watcher.collect_br_rows_needing_ops(values)

    assert rows == [
        {
            "row_number": "3",
            "user_name": "Artist A",
            "region": "BR",
            "track_isrc": "ISRC1",
            "video_link": "https://video/1",
            "revenue_loss": "$10",
            "reason": "Reason 1",
            "contract_type": "Exclusive",
            "product_ops_action": "Need review",
        }
    ]


def test_build_dm_lines_includes_sheet_link_and_video_links():
    rows = [
        {
            "row_number": "3",
            "user_name": "Artist A",
            "region": "BR",
            "track_isrc": "ISRC1",
            "video_link": "https://video/1",
            "revenue_loss": "$10",
            "reason": "Reason 1",
            "contract_type": "Exclusive",
            "product_ops_action": "Need review",
        }
    ]

    lines = watcher.build_dm_lines(rows, "https://sheet.example")

    assert lines[0] == "1 BR row(s) still need ops action in the AMS Content ID Allowlist Approval sheet."
    assert ("link", "Open video link for row 3", "https://video/1") in lines
    assert lines[-1] == ("link", "Open the BR ops sheet", "https://sheet.example")
