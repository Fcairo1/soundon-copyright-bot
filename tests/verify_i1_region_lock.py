#!/usr/bin/env python3
"""I1 verification: REGION_LOCK must never be leaked when manual_scan_region
fails early (e.g. configure_region raises KeyError on an unexpected region).

Steps:
  1. Call manual_scan_region("NOT_A_REGION") and confirm it RAISES.
  2. Confirm REGION_LOCK is NOT held afterwards (acquire non-blocking succeeds).
  3. Confirm a subsequent configure_region("BR") completes and does NOT block.
"""
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert import bot_runtime as br  # noqa: E402


def main() -> int:
    # 1) early failure must raise
    raised = None
    try:
        br.manual_scan_region("NOT_A_REGION")
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None, "manual_scan_region('NOT_A_REGION') did NOT raise"
    print(f"[1] manual_scan_region('NOT_A_REGION') raised {type(raised).__name__}: {raised}")

    # 2) lock must be free (would fail here before the fix — dead thread held it)
    got = br.REGION_LOCK.acquire(blocking=False)
    assert got, "REGION_LOCK is STILL HELD after the early failure — lock was leaked!"
    br.REGION_LOCK.release()
    print("[2] REGION_LOCK is free after the failure (acquire(blocking=False) succeeded)")

    # 3) subsequent configure_region('BR') must complete without blocking
    result = {}
    def _run():
        cfg = br.configure_region("BR")
        result["chat_id"] = cfg["chat_id"]
    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "configure_region('BR') BLOCKED (>10s) — lock deadlock!"
    print(f"[3] configure_region('BR') returned without blocking; chat_id={result.get('chat_id')}")

    print("\nI1 PASS: lock released on early failure; daemon does not freeze.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
