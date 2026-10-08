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


def test_combined_top_n_ranks_across_regions_not_per_region():
    # US's single huge entry must outrank everything else globally, even
    # though it's alone in its region — approved 2026-09-28: one combined
    # ranked list instead of top-5-per-region, after per-region collapsible
    # panels turned out to render with no visible expand affordance in Lark.
    data = {"needs_enforcement": {
        "US": [{"title": "Big US hit", "artist": "A", "label": "", "streams": 16_320_256, "region": "US"}],
        "BR": [{"title": "BR track", "artist": "B", "label": "", "streams": 487_791, "region": "BR"},
               {"title": "BR track 2", "artist": "C", "label": "", "streams": 300_453, "region": "BR"}],
        "SPLA": [{"title": "SPLA track", "artist": "D", "label": "", "streams": 5_532_547, "region": "SPLA"}],
    }}
    top = ad._combined_top_n(data, n=3)
    assert [e["title"] for e in top] == ["Big US hit", "SPLA track", "BR track"]


def test_enforcement_section_md_includes_region_flag_and_rank():
    data = {"needs_enforcement": {"BR": [
        {"title": "Track", "artist": "Artist", "label": "Label", "streams": 1000, "region": "BR"}
    ]}}
    md = ad._enforcement_section_md(data)
    assert "**1.**" in md
    assert "🇧🇷" in md
    assert "[Label]" in md


def test_build_card_stat_tiles_and_region_badges_have_own_background(monkeypatch):
    # Regression test for the real bug found 2026-09-28: background_style on
    # the whole column_set merges all tiles into one flat strip instead of
    # separate cards — it must be set on each column individually.
    data = ad.build_digest_data([AUG3_HEADER])
    card = ad.build_card("Aug3", data, [])
    column_sets = [e for e in card["elements"] if e.get("tag") == "column_set"]
    assert len(column_sets) == 2  # stat tiles row + region badges row
    for cs in column_sets:
        assert "background_style" not in cs  # never on the container
        for col in cs["columns"]:
            assert col.get("background_style") == "grey"  # always per-column


def test_build_card_omits_recovery_section_when_empty_includes_when_present():
    data = ad.build_digest_data([AUG3_HEADER])
    card_empty = ad.build_card("Aug3", data, [])
    text_empty = " ".join(e.get("text", {}).get("content", "") for e in card_empty["elements"] if "text" in e)
    assert "Original-track recovery" not in text_empty

    card_with_recovery = ad.build_card("Aug3", data, ["Track X: +500 streams (+10.0%) since takedown"])
    text_with = " ".join(e.get("text", {}).get("content", "") for e in card_with_recovery["elements"] if "text" in e)
    assert "Original-track recovery" in text_with
    assert "Track X" in text_with


# ── "Do NOT claim" marks (set on the dashboard) ──────────────────────────────

def _flagged_row(song_id, song, m_isrc, m_title, streams, status="Online"):
    return _row(AUG3_HEADER, **{"SO Song ID": song_id, "SO Song Title": song, "SO Region": "BR",
                                "Matched Title": m_title, "Matched ISRC": m_isrc, "Streamings": str(streams),
                                "Status": status, "Decision": "Other Party"})


def test_case_key_matches_the_dashboards_format():
    # acrcloud-dashboard: (so_song_id || song) + "|" + (matched_isrc || matched_title)
    assert ad.case_key("s1", "Song", "ISRC1", "Match") == "s1|ISRC1"
    assert ad.case_key("", "Song", "", "Match") == "Song|Match"


def test_marked_pair_is_left_out_of_enforcement_but_reported():
    rows = [AUG3_HEADER, _flagged_row("s1", "Song A", "ISRC1", "Match 1", 9000),
            _flagged_row("s2", "Song B", "ISRC2", "Match 2", 5000)]
    data = ad.build_digest_data(rows, excluded_keys={"s1|ISRC1"})
    assert [e["title"] for e in data["needs_enforcement"]["BR"]] == ["Match 2"]
    assert data["flagged_total"] == 1
    assert data["do_not_claim"] == 1


def test_unmarked_pair_of_the_same_matched_track_still_needs_enforcement():
    # One infringing track matched against two SoundOn songs; only one pair was
    # vetoed — the other must still show up (exclusion happens before dedupe).
    rows = [AUG3_HEADER, _flagged_row("s1", "Song A", "ISRC1", "Match", 9000),
            _flagged_row("s2", "Song B", "ISRC1", "Match", 4000)]
    data = ad.build_digest_data(rows, excluded_keys={"s1|ISRC1"})
    assert data["flagged_total"] == 1
    assert data["needs_enforcement"]["BR"][0]["streams"] == 4000


def test_marked_pair_that_went_offline_still_counts_as_taken_offline():
    rows = [AUG3_HEADER, _flagged_row("s1", "Song A", "ISRC1", "Match 1", 9000, status="Offline")]
    data = ad.build_digest_data(rows, excluded_keys={"s1|ISRC1"})
    assert data["flagged_total"] == 0 and data["offline_total"] == 1


def test_no_exclusions_behaves_exactly_as_before():
    rows = [AUG3_HEADER, _flagged_row("s1", "Song A", "ISRC1", "Match 1", 9000)]
    data = ad.build_digest_data(rows)
    assert data["flagged_total"] == 1 and data["do_not_claim"] == 0


def test_card_mentions_do_not_claim_only_when_there_are_some():
    rows = [AUG3_HEADER, _flagged_row("s1", "Song A", "ISRC1", "Match 1", 9000)]
    with_marks = ad.build_card("Aug3", ad.build_digest_data(rows, excluded_keys={"s1|ISRC1"}), [])
    without = ad.build_card("Aug3", ad.build_digest_data(rows), [])
    text = lambda c: " ".join(e.get("text", {}).get("content", "") for e in c["elements"] if "text" in e)
    assert "marked do not claim" in text(with_marks)
    assert "do not claim" not in text(without)


def _patch_do_not_claim_tab(monkeypatch, values):
    monkeypatch.setattr(ad, "list_tabs", lambda url: [{"sheet_id": "x1", "name": "Sheet1"}, {"sheet_id": "nc1", "name": "Do Not Claim"}])
    monkeypatch.setattr(ad, "read_tab", lambda url, sid: values)


def test_read_do_not_claim_keys_uses_the_last_event_per_key(monkeypatch):
    header = ["key", "so_song_id", "matched_isrc", "song", "matched_title", "region", "action", "reason", "marked_by", "ts"]
    ev = lambda key, action: _row(header, key=key, action=action)
    _patch_do_not_claim_tab(monkeypatch, [header, ev("a|1", "mark"), ev("b|2", "mark"), ev("a|1", "unmark"), ev("c|3", "mark"), ev("b|2", "unmark"), ev("b|2", "mark")])
    assert ad.read_do_not_claim_keys() == {"b|2", "c|3"}


def test_read_do_not_claim_keys_empty_when_tab_missing(monkeypatch):
    monkeypatch.setattr(ad, "list_tabs", lambda url: [{"sheet_id": "x1", "name": "Sheet1"}])
    assert ad.read_do_not_claim_keys() == set()


def test_read_do_not_claim_keys_failure_does_not_stop_the_digest(monkeypatch):
    monkeypatch.setattr(ad, "list_tabs", lambda url: (_ for _ in ()).throw(RuntimeError("lark down")))
    assert ad.read_do_not_claim_keys() == set()


def _one_tab_digest(monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "DIGEST_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ad, "list_tabs", lambda url: [{"sheet_id": "s1", "name": "Aug3"}])
    monkeypatch.setattr(ad, "read_tab", lambda url, sid: [AUG3_HEADER, _row(AUG3_HEADER, **{
        "SO Song Title": "T1", "SO Region": "BR", "Matched Title": "M1", "Matched ISRC": "I1",
        "Streamings": "1000", "Decision": "Other Party"})])
    monkeypatch.setattr(ad, "build_recovery_lines", lambda: [])
    monkeypatch.setattr(ad, "read_do_not_claim_keys", lambda: set())
    monkeypatch.setattr(ad.ra, "post_card", lambda card, **k: None)


def test_posted_digest_registers_claim_window_once_not_on_dry_run(monkeypatch, tmp_path):
    _one_tab_digest(monkeypatch, tmp_path)
    registered = []
    monkeypatch.setattr(ad, "_register_claim_window", lambda cycle: registered.append(cycle))
    ad.run_daily_check(dry_run=True)
    assert registered == []
    ad.run_daily_check()
    ad.run_daily_check()   # already digested -> no second registration
    assert registered == ["Aug3"]


def test_claim_window_failure_does_not_break_digest(monkeypatch, tmp_path):
    _one_tab_digest(monkeypatch, tmp_path)   # conftest blocks live Lark -> registration raises
    result = ad.run_daily_check()
    assert [p["name"] for p in result["posted"]] == ["Aug3"]
    assert (tmp_path / "state.json").exists()
