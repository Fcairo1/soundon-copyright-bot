#!/usr/bin/env python3
"""H1.3: standalone entrypoint for the proactive JWT health check.

Run on a schedule (cron) so a stale/expired AIME user JWT is discovered and the
operator is alerted BEFORE a DM action-card button click crashes with
"Tracker is empty or unreadable" (H1).

Usage (from the repo root):
    python -m copyright_alert.jwt_healthcheck            # check + alert if stale
    python -m copyright_alert.jwt_healthcheck --no-alert # check only, no DM

Exit code:
    0  JWT healthy (status == ok)
    1  JWT warn/err (stale/expiring/expired) — an operator alert was attempted
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert.bot_runtime import proactive_jwt_healthcheck  # noqa: E402


def main() -> int:
    alert = "--no-alert" not in sys.argv[1:]
    result = proactive_jwt_healthcheck(alert=alert)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
