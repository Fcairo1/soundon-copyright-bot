"""Tests must never touch live Lark sheets through the claim-window module.

copyright_alert.claim_window talks to the production Takedown Stream Tracker
workbook via lark-cli; acr_digest.run_daily_check calls it after posting, so any
test exercising a real (non-dry-run) digest run would otherwise register a
phantom claim window. Tests that need the module patch these names themselves."""
import pytest


@pytest.fixture(autouse=True)
def _block_live_claim_window_io(monkeypatch):
    from copyright_alert import claim_window

    def _blocked(*_a, **_k):
        raise RuntimeError("live Lark access blocked in tests — monkeypatch it in the test")

    for name in ("_run_lark_sheets", "_tab_id", "_read_grid"):
        monkeypatch.setattr(claim_window, name, _blocked)


@pytest.fixture(autouse=True)
def _no_live_status_overrides(monkeypatch):
    """The digest/alarm read the dashboard's Online/Offline corrections from a live
    Lark tab; tests that care patch read_status_overrides themselves."""
    from copyright_alert import acr_digest

    monkeypatch.setattr(acr_digest, "read_status_overrides", lambda: {})
