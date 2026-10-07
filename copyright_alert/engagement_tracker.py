#!/usr/bin/env python3
"""Daily refresh of Spotify + TikTok 30-day engagement for OPEN claims.

The original ``tt_30d_vv`` / ``sptf_30d_str`` tracker columns are a one-time
snapshot taken when the claim was ingested (see
run_alert.enrich_with_engagement_once) and the dashboard reads them as such.
They are deliberately left untouched: the refreshed numbers go into three NEW
columns so the at-claim snapshot survives and cards can show a trend
("↑ 8% since claim"):

  Spotify 30d Streams (Now) · TikTok 30d Views (Now) · Engagement Checked

Which rows are refreshed is decided by the caller-supplied ``is_open``
predicate (daily_workflow passes its own ``_is_open_for_ops`` so "open" means
exactly what it means everywhere else in the daily run). Rows are refreshed
least-recently-checked first, capped per run, so a large backlog rotates
instead of timing the run out.

Runs once a day per region from daily_workflow.py. The in-process ``LATEST``
map lets the same run's countdown-card refresh use the fresh numbers without
re-reading the sheet.
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Callable, Dict, List, Optional

from copyright_alert import handle_callback as hc
from copyright_alert.engagement_format import parse_count
from copyright_alert.marketshare_tracker import (
    _cell,
    _col_letter,
    _header_index,
    _last_used_column_index,
    _norm,
)

UPC_HEADER = "UPC"
STATUS_HEADER = "Status"
ADMIN_ACTION_HEADER = "Admin Action Taken"
RETRACTED_HEADER = "Retracted"

# At-claim snapshot columns (written once at ingestion; never overwritten here).
BASELINE_TIKTOK_HEADER = "tt_30d_vv"
BASELINE_SPOTIFY_HEADER = "sptf_30d_str"
# Refreshed columns owned by this module.
NOW_SPOTIFY_HEADER = "Spotify 30d Streams (Now)"
NOW_TIKTOK_HEADER = "TikTok 30d Views (Now)"
CHECKED_HEADER = "Engagement Checked"
NEW_HEADERS = (NOW_SPOTIFY_HEADER, NOW_TIKTOK_HEADER, CHECKED_HEADER)

# Bounds worst-case runtime (the engagement query is chunked 40 UPCs per Aeolus
# call). Rows beyond the cap are picked up on the next run.
MAX_REFRESH_PER_RUN = int(os.getenv("ENGAGEMENT_MAX_REFRESH_PER_RUN", "300"))

# upc -> {"sptf_now": float|None, "tt_now": float|None, "checked": "YYYY-MM-DD"}
# Populated by run_daily_sync for the rest of this process.
LATEST: Dict[str, Dict[str, object]] = {}


def ensure_columns(region: str) -> Dict[str, object]:
    """Append the new headers after the last real header if missing. Same
    placement logic (and same padded-width pitfall) as
    marketshare_tracker.ensure_columns — see its docstring/regression test."""
    sheet_url, sheet_id = hc._tracker_config(region)
    values = hc.read_sheet_values(region)
    if not values:
        return {"error": "sheet unreadable"}
    headers = [_norm(h) for h in values[0]]
    created = []
    for header_name in NEW_HEADERS:
        if header_name in headers:
            continue
        target_idx = _last_used_column_index(headers) + 1
        while target_idx < len(headers) and headers[target_idx]:
            target_idx += 1
        if target_idx >= len(headers):
            headers.append(header_name)
        else:
            headers[target_idx] = header_name
        hc._sheet_api("PUT", sheet_url, sheet_id, f"{_col_letter(target_idx)}1", values=[[header_name]])
        created.append(header_name)
    return {"created": created}


def _as_cell_number(value) -> Optional[int]:
    n = parse_count(value)
    return None if n is None else int(round(n))


def run_daily_sync(
    region: str,
    *,
    dry_run: bool = False,
    is_open: Optional[Callable[[str, str, str], bool]] = None,
    today: Optional[date] = None,
) -> Dict[str, object]:
    today = today or date.today()
    if is_open is None:
        # Standalone/CLI use: lazy import so this module never imports
        # daily_workflow at load time (daily_workflow imports us).
        from copyright_alert.daily_workflow import _is_open_for_ops as is_open

    ensure_result = ensure_columns(region)
    sheet_url, sheet_id = hc._tracker_config(region)
    values = hc.read_sheet_values(region)
    if not values:
        return {"region": region, "error": "sheet unreadable", "ensure": ensure_result}

    idx = _header_index(values)
    spotify_i, tiktok_i, checked_i = (idx.get(h) for h in NEW_HEADERS)
    if spotify_i is None or tiktok_i is None or checked_i is None:
        return {"region": region, "error": "engagement columns still missing after ensure_columns",
                "ensure": ensure_result}
    upc_i, status_i = idx.get(UPC_HEADER), idx.get(STATUS_HEADER)
    admin_i, retracted_i = idx.get(ADMIN_ACTION_HEADER), idx.get(RETRACTED_HEADER)

    open_rows: List[tuple] = []  # (checked, row_number, upc)
    for row_number, row in enumerate(values[1:], start=2):
        upc = _cell(row, upc_i)
        if not upc or upc.upper() == "N/A":
            continue
        if not is_open(_cell(row, status_i), _cell(row, admin_i), _cell(row, retracted_i)):
            continue
        open_rows.append((_cell(row, checked_i), row_number, upc))

    open_rows.sort(key=lambda item: (item[0], item[1]))  # never-checked first, then oldest
    batch = open_rows[:MAX_REFRESH_PER_RUN]

    from copyright_alert import run_alert as ra  # lazy: heavy, shared with daily_workflow

    found = ra.batch_query_engagement_by_upc([upc for _, _, upc in batch]) if batch else {}

    updates: List[tuple] = []  # (row_number, col_letter, value)
    refreshed = 0
    for _, row_number, upc in batch:
        eng = found.get(upc)
        if not eng:
            continue  # not in the engagement table today — keep whatever is there
        spotify, tiktok = _as_cell_number(eng.get("sptf_30d_str")), _as_cell_number(eng.get("tt_30d_vv"))
        if spotify is None and tiktok is None:
            continue
        if spotify is not None:
            updates.append((row_number, _col_letter(spotify_i), spotify))
        if tiktok is not None:
            updates.append((row_number, _col_letter(tiktok_i), tiktok))
        updates.append((row_number, _col_letter(checked_i), today.isoformat()))
        LATEST[upc] = {"sptf_now": spotify, "tt_now": tiktok, "checked": today.isoformat()}
        refreshed += 1

    if updates and not dry_run:
        from copyright_alert.lark_auth import sheet_values_batch_update

        sheet_values_batch_update(
            sheet_url,
            [{"range": f"{sheet_id}!{col}{row}:{col}{row}", "values": [[val]]} for row, col, val in updates],
        )

    return {
        "region": region, "dry_run": dry_run, "ensure": ensure_result,
        "open_rows": len(open_rows), "queried": len(batch), "refreshed": refreshed,
        "not_found_in_aeolus": len(batch) - refreshed, "pending_next_run": max(0, len(open_rows) - len(batch)),
        "write_count": len(updates),
    }


def run_daily_sync_safe(region: str, *, is_open=None) -> Dict[str, object]:
    """Entry point for daily_workflow.py — never raises."""
    try:
        result = run_daily_sync(region, is_open=is_open)
        print(f"  ✓ Engagement refresh: {json.dumps(result, ensure_ascii=False)}", flush=True)
        return result
    except Exception as exc:
        print(f"  ✗ Engagement refresh failed (non-fatal): {exc!r}", flush=True)
        return {"error": repr(exc)}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Refresh Spotify/TikTok 30d engagement for open claims")
    parser.add_argument("--region", required=True, help="BR, US or SPLA")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_daily_sync(args.region.upper(), dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
