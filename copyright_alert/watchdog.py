#!/usr/bin/env python3
"""Manual auto-heal helper for the copyright alert callback daemon.

NOTE: As of the event-driven refactor, this script is no longer required to
run on a cron / 10-minute polling schedule. The persistent_callback daemon now
detects callback failures inline and recovers itself via
``bot_runtime.ensure_daemon_alive`` (the same shared helper used here). This
file is kept for manual / on-demand health checks only.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert.bot_runtime import watchdog_check_once


def _parse_args():
    parser = argparse.ArgumentParser(description="Restart the callback daemon if it is offline.")
    parser.add_argument("--notify-region", choices=["BR", "US"], default="BR")
    parser.add_argument("--detected-via", choices=["watchdog", "button_click"], default="watchdog")
    parser.add_argument("--no-notify", action="store_true")
    return parser.parse_args()


def main():
    args = _parse_args()
    result = watchdog_check_once(
        send_notifications=not args.no_notify,
        notify_region=args.notify_region,
        detected_via=args.detected_via,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
