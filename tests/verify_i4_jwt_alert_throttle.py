#!/usr/bin/env python3
"""I4 verification: proactive JWT alert must not spam the operator.

The proactive healthcheck runs as a FRESH process on every cron tick, so the
in-memory fingerprint dedupe never fires across runs (and the detail embeds a
changing "expires in Xs" so fingerprints differ anyway). The fix persists a
last-alerted timestamp under runtime/ and re-alerts at most once per interval.

We monkeypatch the Lark send to a no-op success and redirect the state file to a
temp path, then prove:
  1. First alert (fresh) SENDS.
  2. Second alert with a DIFFERENT changing detail, SAME context -> THROTTLED.
  3. A simulated FRESH PROCESS (clear the in-memory set) -> STILL THROTTLED,
     because the throttle is read from disk.
"""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert import lark_auth  # noqa: E402


class _FakeCompleted:
    returncode = 0
    stdout = "ok"
    stderr = ""


def main() -> int:
    # Redirect throttle state to a temp file so we don't touch runtime/.
    tmp = Path(tempfile.mkdtemp()) / "auth_alert_last_sent.json"
    lark_auth._ALERT_STATE_FILE = tmp
    # Simulate a successful Lark send without any network/credentials.
    sends = {"count": 0}
    def _fake_run(cmd, **kwargs):
        sends["count"] += 1
        return _FakeCompleted()
    lark_auth.subprocess.run = _fake_run

    ctx = "proactive JWT healthcheck"

    # 1) first alert on a fresh box -> sends
    r1 = lark_auth.send_stale_token_alert(ctx, "JWT stale (expires in 120s)")
    print(f"[1] first alert: sent={r1}, lark_sends={sends['count']}")
    assert r1 is True and sends["count"] == 1

    # 2) same context, DIFFERENT changing detail -> throttled (no new send)
    r2 = lark_auth.send_stale_token_alert(ctx, "JWT stale (expires in 90s)")
    print(f"[2] second alert (changed detail, same ctx): sent={r2}, lark_sends={sends['count']}")
    assert r2 is False and sends["count"] == 1, "changing detail defeated dedupe (spam!)"

    # 3) simulate a FRESH PROCESS: the in-memory fingerprint set is empty again.
    lark_auth._AUTH_ALERT_FINGERPRINTS.clear()
    r3 = lark_auth.send_stale_token_alert(ctx, "JWT stale (expires in 60s)")
    print(f"[3] fresh-process re-run: sent={r3}, lark_sends={sends['count']}")
    assert r3 is False and sends["count"] == 1, "fresh process re-alerted -> per-cron-run spam!"

    # show the persisted throttle state (the 'last-alerted timestamp')
    print(f"[state] persisted file: {tmp}")
    print(f"[state] contents: {tmp.read_text()}")

    # 4) once the interval elapses, it re-alerts. Rewind the stored ts by >6h.
    import json
    state = json.loads(tmp.read_text())
    state[ctx] = time.time() - (lark_auth._ALERT_MIN_INTERVAL_SEC + 60)
    tmp.write_text(json.dumps(state))
    lark_auth._AUTH_ALERT_FINGERPRINTS.clear()
    r4 = lark_auth.send_stale_token_alert(ctx, "JWT stale (expires in 30s)")
    print(f"[4] after >6h elapsed: sent={r4}, lark_sends={sends['count']}")
    assert r4 is True and sends["count"] == 2, "did not re-alert after the interval elapsed"

    print("\nI4 PASS: at most one operator alert per 6h per context, even across fresh cron processes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
