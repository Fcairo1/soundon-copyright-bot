#!/usr/bin/env python3
"""I2 verification: while a /scan holds REGION_LOCK, a read command (/pending,
/status, /claims, /unassigned) must give the operator feedback instead of
hanging silently.

We simulate a running scan by holding REGION_LOCK in a background thread, then
invoke notify_if_scan_running() (the helper the read commands now call first).
reply_post is monkeypatched to capture EXACTLY what the operator would see.
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert import bot_runtime as br  # noqa: E402

posted = []
br.reply_post = lambda message_id, title, lines: posted.append((title, lines)) or {}


def main() -> int:
    # ---- Case A: NO scan running -> command proceeds, no delay notice ----
    posted.clear()
    delayed = br.notify_if_scan_running("om_fake_msg_A")
    assert delayed is False, "notify_if_scan_running returned True with no scan running"
    assert not posted, f"unexpected operator message when idle: {posted}"
    print("[A] No scan running: notify_if_scan_running=False, no notice posted (command proceeds immediately).")

    # ---- Case B: a /scan holds REGION_LOCK -> operator gets a delay notice ----
    scan_holding = threading.Event()
    release_scan = threading.Event()

    def _fake_scan():
        br.REGION_LOCK.acquire()
        scan_holding.set()
        try:
            release_scan.wait(timeout=15)
        finally:
            br.REGION_LOCK.release()

    t = threading.Thread(target=_fake_scan, daemon=True)
    t.start()
    assert scan_holding.wait(timeout=5), "fake scan failed to acquire lock"

    posted.clear()
    t0 = time.time()
    delayed = br.notify_if_scan_running("om_fake_msg_B")   # operator sends /pending
    elapsed = time.time() - t0

    assert delayed is True, "notify_if_scan_running returned False while scan held the lock"
    assert posted, "operator got NO feedback while scan held the lock"
    title, lines = posted[0]
    print(f"[B] Scan in progress: notify returned True after ~{elapsed:.1f}s (2s probe timeout).")
    print("     Operator sees:")
    print(f"       Title: {title}")
    for ln in lines:
        print(f"       • {ln}")

    release_scan.set()
    t.join(timeout=5)

    print("\nI2 PASS: read commands now surface a 'scan in progress' notice instead of hanging silently.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
