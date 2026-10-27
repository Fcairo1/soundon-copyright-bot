#!/usr/bin/env python3
"""One-time historical backfill for Spotify claim retraction emails.

Usage: python scripts/backfill/backfill_retracted_claims.py [--dry-run] [BR SPLA US]
--dry-run only logs what it would mark; it writes nothing and saves no state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import daily_workflow as dw  # noqa: E402

REGIONS = ("BR", "SPLA", "US")


def backfill_region(region: str, dry_run: bool = False) -> dict:
    dw.configure_region(region)
    values = dw.read_sheet_values("A:AA")
    dw.ensure_admin_action_column(values)
    dw.ensure_retracted_column(values)
    dw.ensure_spotify_columns(values)
    values = dw.read_sheet_values("A:AA")
    result = dw.run_retraction_pass(values, full_history=True, notify=False, dry_run=dry_run)
    return result


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a.strip()]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if args:
        regions = tuple(arg.strip().upper() for arg in args)
    else:
        regions = REGIONS

    summary = {}
    for region in regions:
        summary[region] = backfill_region(region, dry_run=dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
