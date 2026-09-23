from copyright_alert import acr_digest as ad

AUG3_HEADER = ["SO FileName", "SO Song Title", "SO Song ID", "SO ISRC", "operation_manager_list",
               "SO Artist", "SO Release Date", "SO Region", "SO Streamings"] + ["x"] * 11 + [
               "Matched Title", "Matched Artists", "Matched Album", "Matched Label", "Matched ISRC"] + [
               "x"] * 22 + ["Streamings"] + ["x"] * 6 + ["Status", "Decision"]

AUG19_HEADER = ["SO FileName", "SO Song Title", "SO Song ID", "SO ISRC", "SO User ID", "operation_manager_list",
                "SO Artist", "SO Release Date", "SO Region", "SO Streamings"] + ["x"] * 10 + [
                "Matched Title", "Matched Artists", "Matched Album", "Matched Label", "Matched ISRC"] + [
                "x"] * 23 + ["Streamings"] + ["x"] * 5 + ["Status", "Review Result"]


def _row(header, **values):
    row = [""] * len(header)
    idx = {h: i for i, h in enumerate(header)}
    for k, v in values.items():
        row[idx[k]] = v
    return row


def test_header_aliases_across_real_schema_drift():
    idx3, idx19 = ad._header_index(AUG3_HEADER), ad._header_index(AUG19_HEADER)
    assert ad._find(idx3, *ad.DECISION_HEADER_ALIASES) == idx3["Decision"]
    assert ad._find(idx19, *ad.DECISION_HEADER_ALIASES) == idx19["Review Result"]
    assert ad.is_valid_cycle_tab(idx3) and ad.is_valid_cycle_tab(idx19)


def test_invalid_tab_missing_headers():
    assert not ad.is_valid_cycle_tab(ad._header_index(["UPC", "Status"]))
    assert not ad.is_valid_cycle_tab(ad._header_index(["#REF!"] + [""] * 19))


def test_clean_names_strips_json_array_artifacts():
    assert ad._clean_names('["Detagaz","MC Guh SR"]') == "Detagaz and MC Guh SR"
    assert ad._clean_names('El Bogueto') == "El Bogueto"


def test_parse_streams_handles_commas_and_blank():
    assert ad._parse_streams("5,532,547") == 5532547
    assert ad._parse_streams("") == 0
    assert ad._parse_streams("9") == 9


def test_decision_matching_is_substring_case_insensitive():
    assert ad._is_other_party("Other Party") and ad._is_other_party("Other Party infringing on us")
    assert not ad._is_other_party("Own Release")
    assert ad._is_additional_review("Additional Review")
    assert not ad._is_additional_review("Other Party")


def test_build_digest_data_real_world_shape():
    rows = [AUG3_HEADER]
    rows.append(_row(AUG3_HEADER, **{"SO Song Title": "T1", "SO Region": "US", "Matched Title": "M1",
                                     "Matched Artists": '["A1"]', "Matched Label": "L1", "Matched ISRC": "ISRC1",
                                     "Streamings": "3,731,662", "Status": "Online", "Decision": "Other Party"}))
    rows.append(_row(AUG3_HEADER, **{"SO Song Title": "T2", "SO Region": "BR", "Matched Title": "M2",
                                     "Matched Artists": '["A2"]', "Matched Label": "L2", "Matched ISRC": "ISRC2",
                                     "Streamings": "374,301", "Status": "Offline", "Decision": "Other Party"}))
    rows.append(_row(AUG3_HEADER, **{"SO Song Title": "T3", "SO Region": "MX", "Matched Title": "M3",
                                     "Matched Artists": '["A3"]', "Matched Label": "L3", "Matched ISRC": "ISRC3",
                                     "Streamings": "5,532,547", "Status": "Online", "Decision": "Other Party"}))
    rows.append(_row(AUG3_HEADER, **{"SO Song Title": "T4", "Decision": "Additional Review"}))
    rows.append(_row(AUG3_HEADER, **{"SO Song Title": "T5", "Decision": "No Infringement"}))

    data = ad.build_digest_data(rows)
    assert data["total_reviewed"] == 5   # every scanned row, not just decision-labeled ones
    assert data["escalated"] == 1
    assert data["flagged_total"] == 3
    assert set(data["needs_enforcement"].keys()) == {"US", "BR", "SPLA"}
    assert data["needs_enforcement"]["SPLA"][0]["streams"] == 5532547
    assert data["offline_total"] == 1
    assert data["offline_top"]["matched_title"] == "M2"


def test_build_digest_data_caps_at_five_per_region_sorted_by_streams():
    rows = [AUG3_HEADER]
    for i in range(8):
        rows.append(_row(AUG3_HEADER, **{"SO Song Title": f"T{i}", "SO Region": "BR",
                                         "Matched Title": f"M{i}", "Matched ISRC": f"ISRC{i}",
                                         "Streamings": str(1000 * (i + 1)), "Decision": "Other Party"}))
    data = ad.build_digest_data(rows)
    assert data["enforcement_counts"]["BR"] == 8
    top = data["needs_enforcement"]["BR"]
    assert len(top) == 5
    assert [e["streams"] for e in top] == [8000, 7000, 6000, 5000, 4000]


def test_build_digest_data_dedupes_by_matched_isrc_keeping_max_streams():
    rows = [AUG3_HEADER]
    rows.append(_row(AUG3_HEADER, **{"SO Song Title": "T1", "SO Region": "US", "Matched Title": "Same",
                                     "Matched ISRC": "SAME_ISRC", "Streamings": "100", "Decision": "Other Party"}))
    rows.append(_row(AUG3_HEADER, **{"SO Song Title": "T2", "SO Region": "US", "Matched Title": "Same",
                                     "Matched ISRC": "SAME_ISRC", "Streamings": "500", "Decision": "Other Party"}))
    data = ad.build_digest_data(rows)
    assert data["flagged_total"] == 1
    assert data["needs_enforcement"]["US"][0]["streams"] == 500


def test_build_digest_data_no_flagged_cases():
    rows = [AUG3_HEADER, _row(AUG3_HEADER, **{"SO Song Title": "T1", "Decision": "No Infringement"})]
    data = ad.build_digest_data(rows)
    assert data["flagged_total"] == 0 and data["needs_enforcement"] == {}
    assert "None flagged" in ad._enforcement_section_md(data)


def test_total_reviewed_counts_undecided_rows_too():
    rows = [AUG3_HEADER, _row(AUG3_HEADER, **{"SO Song Title": "T1"}), _row(AUG3_HEADER, **{"SO Song Title": "T2"})]
    assert ad.build_digest_data(rows)["total_reviewed"] == 2


def test_card_has_dashboard_link_button():
    data = ad.build_digest_data([AUG3_HEADER])
    card = ad.build_card("Aug3", data, [])
    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions and actions[0]["actions"][0]["url"] == ad.DASHBOARD_URL


def test_run_daily_check_skips_invalid_and_already_digested(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "DIGEST_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ad, "list_tabs", lambda url: [
        {"sheet_id": "s1", "name": "Aug3"}, {"sheet_id": "s2", "name": "Flags"}, {"sheet_id": "s3", "name": "Sep22"},
    ])

    def fake_read(url, sheet_id):
        if sheet_id == "s1":
            return [AUG3_HEADER, _row(AUG3_HEADER, **{"SO Song Title": "T1", "SO Region": "BR",
                                                      "Matched Title": "M1", "Matched ISRC": "I1",
                                                      "Streamings": "1000", "Decision": "Other Party"})]
        if sheet_id == "s2":
            return [["#hidden", ""] + [""] * 18]
        return [["#REF!"] + [""] * 19]

    monkeypatch.setattr(ad, "read_tab", fake_read)
    monkeypatch.setattr(ad, "build_recovery_lines", lambda: [])
    posted = []
    monkeypatch.setattr(ad.ra, "post_card", lambda card, **k: posted.append(k))

    result = ad.run_daily_check()
    assert [p["name"] for p in result["posted"]] == ["Aug3"]
    assert len(posted) == 1 and posted[0]["chat_id"] == ad.CONTENT_SAFETY_CHAT_ID
    assert {s["name"] for s in result["skipped_invalid"]} == {"Flags", "Sep22"}

    result2 = ad.run_daily_check()
    assert result2["posted"] == [] and result2["already_digested"] == ["Aug3"]
    assert len(posted) == 1


def test_run_daily_check_dry_run_does_not_post_or_persist(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(ad, "DIGEST_STATE_FILE", state_file)
    monkeypatch.setattr(ad, "list_tabs", lambda url: [{"sheet_id": "s1", "name": "Aug3"}])
    monkeypatch.setattr(ad, "read_tab", lambda url, sid: [AUG3_HEADER, _row(AUG3_HEADER, **{
        "SO Song Title": "T1", "SO Region": "BR", "Matched Title": "M1", "Matched ISRC": "I1",
        "Streamings": "1000", "Decision": "Other Party"})])
    monkeypatch.setattr(ad, "build_recovery_lines", lambda: [])
    monkeypatch.setattr(ad.ra, "post_card", lambda card, **k: (_ for _ in ()).throw(AssertionError("must not post")))

    result = ad.run_daily_check(dry_run=True)
    assert [p["name"] for p in result["posted"]] == ["Aug3"]
    assert not state_file.exists()


def test_build_recovery_lines_from_real_column_layout():
    header = ["so_song_id", "so_isrc", "song", "artist", "matched_title", "matched_isrc",
              "region", "takedown_date", "baseline_7d_streams", "latest_7d_streams",
              "streams_delta_abs", "streams_delta_pct", "last_checked"]
    rows = [header,
            _row(header, song="Track A", streams_delta_abs="120", streams_delta_pct="15.0", last_checked="2026-09-20 10:00:00"),
            _row(header, song="Track B", streams_delta_abs="-500", streams_delta_pct="-40.0", last_checked="2026-09-20 10:00:00"),
            _row(header, song="Track C", streams_delta_abs="10", streams_delta_pct="1.0", last_checked="")]

    import copyright_alert.acr_digest as ad_module
    orig = ad_module.read_tab
    ad_module.read_tab = lambda url, sid: rows
    try:
        lines = ad_module.build_recovery_lines()
    finally:
        ad_module.read_tab = orig
    assert len(lines) == 2
    assert "Track B" in lines[0]
