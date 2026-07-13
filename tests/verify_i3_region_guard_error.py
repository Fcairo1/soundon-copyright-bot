#!/usr/bin/env python3
"""I3 verification: region guards must raise RegionGuardError (a normal
Exception), NOT SystemExit.

Confirms:
  (a) daily_workflow's `except Exception` section wrapper CATCHES a tripped guard,
      so the run continues to the NEXT section (SystemExit would have escaped and
      aborted the whole run).
  (b) a daemon worker thread SURFACES an error reply instead of dying silently
      (SystemExit is swallowed by threads -> silent death).
"""
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert.region_guard import (  # noqa: E402
    RegionGuardError,
    assert_chat_matches_region,
)

BR_CHAT = "oc_6e157309d8d7145ba5ce7f0ba67354cb"  # scoped to {BR}


def main() -> int:
    # Sanity: the guard now raises RegionGuardError, and it is a normal Exception
    # (so `except Exception` catches it) and is NOT a SystemExit.
    assert issubclass(RegionGuardError, Exception)
    assert not issubclass(RegionGuardError, SystemExit)
    tripped = None
    try:
        # intended region US posted to the BR-only chat -> mismatch -> trip
        assert_chat_matches_region(BR_CHAT, "US", context="I3 test countdown replacement")
    except RegionGuardError as exc:
        tripped = exc
    except SystemExit as exc:  # pragma: no cover - would mean the fix regressed
        raise AssertionError(f"guard still raises SystemExit: {exc}")
    assert tripped is not None, "guard did not trip on a region mismatch"
    print(f"[type] guard raised RegionGuardError (Exception subclass, not SystemExit): {tripped}")

    # ---- (a) daily_workflow-style section loop continues past a tripped guard ----
    ran_sections = []

    def section_scan():
        ran_sections.append("scan")

    def section_countdown_replacement():
        # this is the path that trips the guard (H2 countdown replacement card)
        assert_chat_matches_region(BR_CHAT, "US", context="countdown replacement card")

    def section_dm_action_cards():
        ran_sections.append("dm_action_cards")

    for name, fn in [
        ("scan", section_scan),
        ("countdown_replacement", section_countdown_replacement),
        ("dm_action_cards", section_dm_action_cards),
    ]:
        try:
            fn()
        except Exception as e:  # same wrapper daily_workflow.main() uses
            print(f"     ✗ Section '{name}' error caught & logged: {type(e).__name__}: {str(e)[:70]}...")

    assert "dm_action_cards" in ran_sections, "later section did NOT run — guard aborted the whole run!"
    print(f"[a] daily workflow continued to later sections after the trip: ran={ran_sections}")

    # ---- (b) daemon worker thread surfaces an error instead of dying silently ----
    surfaced = []

    def _worker():
        try:
            assert_chat_matches_region(BR_CHAT, "US", context="daemon manual scan group post")
        except Exception as exc:  # start_scan_in_background uses this same pattern
            surfaced.append(f"/scan failed: {repr(exc)}")

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)
    assert surfaced, "daemon worker died SILENTLY (no error surfaced) — SystemExit regression"
    print(f"[b] daemon worker surfaced an operator error instead of dying silently:")
    print(f"     {surfaced[0][:110]}...")

    print("\nI3 PASS: RegionGuardError is catchable; daily run continues; daemon threads report the block.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
