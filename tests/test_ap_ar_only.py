"""Since Q3 2026 only AP / A&R accounts count: the High Quality tier is not a reference."""
from copyright_alert import bot_runtime, daily_workflow, metadata_notice as mn, run_alert as ra


def row(source, tier="Basic", region="BR"):
    return {"user_region": region, "source_type_name": source, "User Tier": tier}


def test_high_quality_non_ap_account_no_longer_qualifies(monkeypatch):
    monkeypatch.setattr(ra, "QUALIFY_COUNTRIES", {"BR"})
    assert ra.qualifies(row("Organic", "High Quality")) is False
    assert ra.qualifies(row("UG-Paid ads", "High Quality")) is False
    assert ra.qualifies(row("Open Service", "High Quality")) is False


def test_ap_and_ar_still_qualify_whatever_the_tier(monkeypatch):
    monkeypatch.setattr(ra, "QUALIFY_COUNTRIES", {"BR"})
    assert ra.qualifies(row("AP", "Basic")) is True
    assert ra.qualifies(row("A&R", "Normal")) is True
    assert ra.qualifies(row("AP", "High Quality")) is True


def test_region_is_still_required(monkeypatch):
    monkeypatch.setattr(ra, "QUALIFY_COUNTRIES", {"BR"})
    assert ra.qualifies(row("AP", region="ID")) is False


def test_manual_scan_gate_matches():
    assert bot_runtime.qualifies_region(row("Organic", "High Quality"), "BR") is False
    assert bot_runtime.qualifies_region(row("AP"), "BR") is True
    assert bot_runtime.qualifies_region(row("A&R", region="MX"), "SPLA") is True
    assert bot_runtime.qualifies_region(row("UG-SEO", "High Quality", region="US"), "US") is False


def _metadata_setup(monkeypatch, aeolus, status):
    monkeypatch.setattr(mn, "query_aeolus", lambda upc: aeolus)
    monkeypatch.setattr(mn, "resolve_notice_region", lambda *a, **k: ("BR" if aeolus else "", aeolus, status))
    monkeypatch.setattr(mn, "parse_metadata_notice",
                        lambda body, subject, meta: {"upc": "5064000000001", "ref_id": "ref:_z:ref", "subject": subject})
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write/DM"))  # noqa: E731
    monkeypatch.setattr(mn, "_append_metadata_correction_row", boom)
    monkeypatch.setattr(mn, "_send_notice_dm", boom)


def test_metadata_notice_for_a_high_quality_non_ap_account_is_dropped(monkeypatch):
    _metadata_setup(monkeypatch, row("Organic", "High Quality"), "ok")
    result = mn.handle_metadata_notice("body", "Metadata", {}, msg_id="m1")
    assert result["status"] == "ineligible_source" and result["source_type_name"] == "Organic"


def test_metadata_notice_without_aeolus_data_is_not_registered(monkeypatch):
    _metadata_setup(monkeypatch, {}, "no_aeolus_row")
    assert mn.handle_metadata_notice("body", "Metadata", {}, msg_id="m1")["status"] == "no_aeolus_row"


def test_scan_retries_a_metadata_notice_with_no_aeolus_data(monkeypatch):
    monkeypatch.setattr(daily_workflow, "fetch_email", lambda mid: ("body", {}))
    monkeypatch.setattr(daily_workflow.metadata_notice, "is_metadata_notice", lambda *a: True)
    monkeypatch.setattr(daily_workflow.metadata_notice, "handle_metadata_notice",
                        lambda *a, **k: {"status": "no_aeolus_row"})
    summary = {"skipped_prefilter": 0, "examined": 0, "skipped_no_identifier": 0, "skipped_non_claim": 0}
    kind, _ = daily_workflow._parse_candidate("m1", "Takedown Notification", "2026-11-02T10:00:00Z", "t1", set(), summary)
    assert kind == "failed"
