#!/usr/bin/env python3
"""Daily refresh of Spotify + TikTok 30-day engagement for OPEN claims.

The tracker's existing ``tt_30d_vv`` (F) and ``sptf_30d_str`` (G) columns are
refreshed IN PLACE, so they hold the CURRENT figures. They used to be a one-time
snapshot taken at ingestion; to keep that original value (needed for the
"↓ since claim" trend and so a taken-down big track keeps its 🔥 — the 30d
window collapses after a takedown) one new column preserves it:

  Spotify At Claim   — the G value as it was before the first overwrite

One-time migration, done by the same daily run and safe to repeat: for every
row with a UPC and a blank "Spotify At Claim", copy the current G value into it
(or "N/A" when G has none, so it is never blank again and can never later
capture a refreshed figure by mistake). That write is read back and verified
before any F/G cell is overwritten; rows whose copy didn't persist are skipped
this run. New rows get the column filled at ingestion (run_alert).

Which rows are refreshed is decided by the caller-supplied ``is_open``
predicate (daily_workflow passes its own ``_is_open_for_ops``). Rows are
refreshed newest-first up to a per-run cap.

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

# Existing columns, now refreshed in place with the CURRENT 30d figures.
TIKTOK_HEADER = "tt_30d_vv"
SPOTIFY_HEADER = "sptf_30d_str"
# The one new column: Spotify 30d streams as captured when the claim came in.
AT_CLAIM_HEADER = "Spotify At Claim"
NEW_HEADERS = (AT_CLAIM_HEADER,)

# Bounds worst-case runtime (the engagement query is chunked 40 UPCs per Aeolus
# call). Rows beyond the cap are picked up on the next run.
MAX_REFRESH_PER_RUN = int(os.getenv("ENGAGEMENT_MAX_REFRESH_PER_RUN", "300"))

# upc -> {"sptf_now": float|None, "tt_now": float|None, "checked": "YYYY-MM-DD"}
# Populated by run_daily_sync for the rest of this process.
LATEST: Dict[str, Dict[str, object]] = {}


def ensure_columns(region: str) -> Dict[str, object]:
    """Append the "Spotify At Claim" header after the last real header if missing.
    Same placement logic (and same padded-width pitfall) as
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


def _write(sheet_url: str, sheet_id: str, cells: List[tuple]) -> None:
    from copyright_alert.lark_auth import sheet_values_batch_update

    if cells:
        sheet_values_batch_update(
            sheet_url,
            [{"range": f"{sheet_id}!{col}{row}:{col}{row}", "values": [[val]]} for row, col, val in cells],
        )


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

    sheet_url, sheet_id = hc._tracker_config(region)
    # A dry run must not touch the sheet at all (it used to create headers).
    ensure_result = {"created": [], "skipped": "dry_run"} if dry_run else ensure_columns(region)
    values = hc.read_sheet_values(region)
    if not values:
        return {"region": region, "error": "sheet unreadable", "ensure": ensure_result}

    idx = _header_index(values)
    tiktok_i, spotify_i, at_claim_i = idx.get(TIKTOK_HEADER), idx.get(SPOTIFY_HEADER), idx.get(AT_CLAIM_HEADER)
    if tiktok_i is None or spotify_i is None:
        return {"region": region, "error": f"{TIKTOK_HEADER}/{SPOTIFY_HEADER} columns not found", "ensure": ensure_result}
    if at_claim_i is None and not dry_run:
        return {"region": region, "error": f"'{AT_CLAIM_HEADER}' column still missing after ensure_columns",
                "ensure": ensure_result}
    upc_i, status_i = idx.get(UPC_HEADER), idx.get(STATUS_HEADER)
    admin_i, retracted_i = idx.get(ADMIN_ACTION_HEADER), idx.get(RETRACTED_HEADER)

    # --- step 1: preserve the at-claim value BEFORE any F/G cell is overwritten -----
    backfill: List[tuple] = []   # (row_number, col_letter, value)
    for row_number, row in enumerate(values[1:], start=2):
        if not _cell(row, upc_i) or at_claim_i is None:
            continue
        if _cell(row, at_claim_i):
            continue                      # already preserved — never overwritten again
        current = _cell(row, spotify_i)
        backfill.append((row_number, _col_letter(at_claim_i),
                         _as_cell_number(current) if _as_cell_number(current) is not None else "N/A"))

    if backfill and not dry_run:
        _write(sheet_url, sheet_id, backfill)
        values = hc.read_sheet_values(region)          # verify it actually persisted
        if not values:
            return {"region": region, "error": "sheet unreadable after at-claim backfill", "ensure": ensure_result,
                    "at_claim_backfilled": len(backfill)}

    # --- step 2: refresh F/G for open rows whose at-claim value is safely stored ----
    open_rows: List[tuple] = []  # (row_number, upc)
    skipped_unpreserved = 0
    for row_number, row in enumerate(values[1:], start=2):
        upc = _cell(row, upc_i)
        if not upc or upc.upper() == "N/A":
            continue
        if not is_open(_cell(row, status_i), _cell(row, admin_i), _cell(row, retracted_i)):
            continue
        if not dry_run and not _cell(row, at_claim_i):
            skipped_unpreserved += 1          # backfill didn't persist — don't lose the original
            continue
        open_rows.append((row_number, upc))

    open_rows.sort(key=lambda item: -item[0])  # newest rows first when capped
    batch = open_rows[:MAX_REFRESH_PER_RUN]

    from copyright_alert import run_alert as ra  # lazy: heavy, shared with daily_workflow

    found = ra.batch_query_engagement_by_upc([upc for _, upc in batch]) if batch else {}

    updates: List[tuple] = []  # (row_number, col_letter, value)
    refreshed = 0
    for row_number, upc in batch:
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
        LATEST[upc] = {"sptf_now": spotify, "tt_now": tiktok, "checked": today.isoformat()}
        refreshed += 1

    if updates and not dry_run:
        _write(sheet_url, sheet_id, updates)

    return {
        "region": region, "dry_run": dry_run, "ensure": ensure_result,
        "at_claim_backfilled": len(backfill), "skipped_at_claim_not_persisted": skipped_unpreserved,
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
