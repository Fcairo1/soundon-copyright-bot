from datetime import date

from copyright_alert import marketshare as ms


def test_bucket_lost_confirm_takedown():
    assert ms.bucket("🔴 Confirm Takedown", "", "") == "lost"


def test_bucket_lost_resolved_agree():
    assert ms.bucket("✅ Resolved", "Sent ✅ – agree – 2026-06-19 11:54:00", "") == "lost"


def test_bucket_protected_resolved_not_agree():
    assert ms.bucket("✅ Resolved", "Draft 📝 – investigating – 2026-06-22 13:17 BRT", "") == "protected"


def test_bucket_protected_retracted_overrides_status():
    assert ms.bucket("🔴 Confirm Takedown", "", "Yes") == "protected"


def test_bucket_protected_fraudulent():
    assert ms.bucket("🚫 Fraudulent", "", "") == "protected"


def test_bucket_at_risk_investigating_and_disputing():
    assert ms.bucket("🔍 Investigating", "", "") == "at_risk"
    assert ms.bucket("⚖️ Disputing", "", "") == "at_risk"


def test_bucket_none_when_no_status():
    assert ms.bucket("", "", "") is None


def test_is_open_matches_at_risk_bucket():
    assert ms.is_open("🔍 Investigating", "", "")
    assert not ms.is_open("🔴 Confirm Takedown", "", "")
    assert not ms.is_open("", "", "")


def _rows(cols, data):
    return {"data": {"columns": cols, "data": data}}


def test_resolve_upc_picks_most_recent_country(monkeypatch):
    ms._upc_cache.clear()
    monkeypatch.setattr(ms.ra, "_run_aeolus_sql", lambda sql, **k: _rows(
        ["upc", "isrc", "user_region", "latest"],
        [["638022638262", "A1", "BR", "2026-09-10"], ["638022638262", "A2", "BR", "2026-09-10"],
         ["638022638262", "A1", "US", "2026-08-01"]],
    ))
    resolved = ms.resolve_upc("638022638262")
    assert resolved["isrcs"] == ["A1", "A2"]
    assert resolved["country"] == "BR"


def test_resolve_upc_none_for_blank_or_na():
    assert ms.resolve_upc("") is None
    assert ms.resolve_upc("N/A") is None


def test_resolve_upc_none_when_aeolus_has_no_rows(monkeypatch):
    monkeypatch.setattr(ms.ra, "_run_aeolus_sql", lambda sql, **k: _rows(["upc", "isrc", "user_region", "latest"], []))
    assert ms.resolve_upc("000000000000") is None


def test_resolve_upc_is_cached(monkeypatch):
    calls = []
    def fake(sql, **k):
        calls.append(sql)
        return _rows(["upc", "isrc", "user_region", "latest"], [["123", "A1", "BR", "2026-09-10"]])
    monkeypatch.setattr(ms.ra, "_run_aeolus_sql", fake)
    ms._upc_cache.clear()
    ms.resolve_upc("123")
    ms.resolve_upc("123")
    assert len(calls) == 1


def test_batch_resolve_upcs_chunks_and_dedupes(monkeypatch):
    calls = []
    def fake(sql, **k):
        calls.append(sql)
        return _rows(["upc", "isrc", "user_region", "latest"],
                     [["U1", "A1", "BR", "2026-09-10"], ["U2", "B1", "US", "2026-09-10"]])
    monkeypatch.setattr(ms.ra, "_run_aeolus_sql", fake)
    ms._upc_cache.clear()
    out = ms.batch_resolve_upcs(["U1", "U2", "U1", ""], chunk_size=40)
    assert len(calls) == 1   # one query for both UPCs
    assert out["U1"] == {"isrcs": ["A1"], "country": "BR", "fetched_at": out["U1"]["fetched_at"]}
    assert out["U2"]["country"] == "US"


def test_batch_resolve_upcs_missing_upc_is_none(monkeypatch):
    monkeypatch.setattr(ms.ra, "_run_aeolus_sql", lambda sql, **k: _rows(["upc", "isrc", "user_region", "latest"], []))
    ms._upc_cache.clear()
    assert ms.batch_resolve_upcs(["ghost"]) == {"ghost": None}


def test_marketshare_ppm_divides_correctly(monkeypatch):
    calls = []

    def fake(sql, **k):
        calls.append(sql)
        return _rows(["n", "d"], [["1006", "17813848870.595703"]])

    monkeypatch.setattr(ms.ra, "_run_aeolus_sql", fake)
    ppm = ms._marketshare_ppm(["AEA3A2209422"], "BR", date(2026, 9, 12))
    assert ppm == round(1006 / 17813848870.595703 * 1_000_000, 3)
    assert len(calls) == 1   # one combined query, not two — this is the fix for the 600s timeout
    assert "2026-08-14" in calls[0] and "2026-09-12" in calls[0]   # 30-day window


def test_marketshare_ppm_none_on_zero_denominator(monkeypatch):
    monkeypatch.setattr(ms.ra, "_run_aeolus_sql", lambda sql, **k: _rows(["n", "d"], [["100", "0"]]))
    assert ms._marketshare_ppm(["X"], "BR", date(2026, 9, 12)) is None


def test_marketshare_ppm_none_for_empty_isrcs_or_country():
    assert ms._marketshare_ppm([], "BR", date(2026, 9, 12)) is None
    assert ms._marketshare_ppm(["X"], "", date(2026, 9, 12)) is None


def test_get_marketshare_ppm_for_upc_none_when_unresolved(monkeypatch):
    monkeypatch.setattr(ms, "resolve_upc", lambda upc: None)
    assert ms.get_marketshare_ppm_for_upc("bad", date(2026, 9, 12)) is None


def test_batch_groups_shared_isrc_sets_into_one_query(monkeypatch):
    ms._upc_cache.clear()
    monkeypatch.setattr(ms, "resolve_upc", lambda upc: {"isrcs": ["A1"], "country": "BR"} if upc != "other" else {"isrcs": ["Z9"], "country": "US"})
    calls = []
    def fake_compute(isrcs, country, end_date, **k):
        calls.append((tuple(isrcs), country))
        return 5.0
    monkeypatch.setattr(ms, "_marketshare_ppm", fake_compute)
    out = ms.get_marketshare_ppm_for_upcs(["upc1", "upc2", "other", "upc1"], date(2026, 9, 12))
    assert out == {"upc1": 5.0, "upc2": 5.0, "other": 5.0}
    assert len(calls) == 2   # one query for the (BR, A1) group, one for (US, Z9)


def test_batch_none_for_unresolvable_upc(monkeypatch):
    monkeypatch.setattr(ms, "resolve_upc", lambda upc: None)
    out = ms.get_marketshare_ppm_for_upcs(["bad"], date(2026, 9, 12))
    assert out == {"bad": None}
