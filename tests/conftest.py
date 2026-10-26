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


@pytest.fixture(autouse=True)
def _never_talk_to_live_lark(monkeypatch):
    """No test may post cards/DMs, patch messages or call lark-cli for real.

    Several tests build real notification cards with obviously fake data (UPC 999,
    title "T"); when the suite runs on a machine that has live bot credentials
    (the AIME sandbox does) those went out as real DMs to ops. Tests that need a
    sender patch it themselves — that overrides these guards."""
    import subprocess
    from copyright_alert import bot_runtime

    def _blocked_post(path, body=None, method="POST"):
        raise RuntimeError(f"live Lark API call blocked in tests: {method} {path}")

    monkeypatch.setattr(bot_runtime, "_post_api", _blocked_post)

    real_run = subprocess.run

    def _guarded_run(cmd, *a, **k):
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        if argv and str(argv[0]).split("/")[-1] == "lark-cli":
            raise RuntimeError(f"live lark-cli call blocked in tests: {' '.join(map(str, argv[:4]))}")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", _guarded_run)
