#!/usr/bin/env python3
"""Daily sync for the two marketshare tracker columns (see marketshare.py for
the metric definition and bucket rules).

Backfill: any tracker row with a UPC but no "Marketshare At Claim (ppm)" gets
one, using its OWN Date Received as the 30d-window end date (a real historical
snapshot, not today's number relabeled).

Refresh: any row currently in the "at_risk" bucket (blank-but-posted /
Investigating / Disputing) gets "Marketshare Current (ppm)" recomputed against
today. Lost/protected claims are frozen at their at-claim value and are not
re-queried here — the dashboard shows at-claim for those buckets.

Deliberately NOT hooked into claim ingestion (run_alert.py posts claims via
several code paths — the incremental scan, manual reposts, etc. — and hooking
all of them is fragile); this runs once a day per region from
daily_workflow.py, same shape as claim_events.py and takedown_stream_tracker.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from copyright_alert import handle_callback as hc
from copyright_alert import marketshare as ms

UPC_HEADER = "UPC"
STATUS_HEADER = "Status"
DATE_RECEIVED_HEADER = "Date Received"
EMAIL_STATUS_HEADER = "Email Status"
RETRACTED_HEADER = "Retracted"


def _norm(value) -> str:
    return str(value or "").strip()


def _header_index(values) -> Dict[str, int]:
    return {_norm(v): i for i, v in enumerate(values[0])} if values else {}


def _cell(row: Sequence[object], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return _norm(row[idx])


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _last_used_column_index(headers: Sequence[str]) -> int:
    """Index of the last non-blank header. read_sheet_values pads every row
    out to a fixed width (see handle_callback.SHEET_READ_WIDTH), so
    len(headers) is that fixed width, NOT the real number of columns in use —
    using it as "the next free column" targets a column far past the real
    data (or, if the real headers ever exceeded the old, narrower pad width,
    an ALREADY-USED one: this is exactly how "Marketshare At Claim (ppm)"
    overwrote the live "Retracted" header on the BR tracker on 2026-09-22).
    Mirrors daily_workflow.ensure_tracker_column's last_nonempty scan."""
    last = -1
    for i, h in enumerate(headers):
        if h:
            last = i
    return last


def ensure_columns(region: str) -> Dict[str, object]:
    """Append the two marketshare headers to the region's tracker if missing.
    Mirrors daily_workflow.ensure_tracker_column, self-contained here so this
    module doesn't depend on daily_workflow's ACTIVE_REGION global state."""
    sheet_url, sheet_id = hc._tracker_config(region)
    values = hc.read_sheet_values(region)
    if not values:
        return {"error": "sheet unreadable"}
    headers = [_norm(h) for h in values[0]]
    created = []
    for header_name in (ms.MARKETSHARE_AT_CLAIM_HEADER, ms.MARKETSHARE_CURRENT_HEADER):
        if header_name in headers:
            continue
        target_idx = _last_used_column_index(headers) + 1
        while target_idx < len(headers) and headers[target_idx]:
            target_idx += 1   # defensive: skip past anything already there
        if target_idx >= len(headers):
            headers.append(header_name)
        else:
            headers[target_idx] = header_name
        hc._sheet_api("PUT", sheet_url, sheet_id, f"{_col_letter(target_idx)}1", values=[[header_name]])
        created.append(header_name)
    return {"created": created}


def _parse_date(value: str) -> Optional[date]:
    text = _norm(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def run_daily_sync(region: str, *, dry_run: bool = False, today: Optional[date] = None) -> Dict[str, object]:
    today = today or date.today()
    ensure_result = ensure_columns(region)
    sheet_url, sheet_id = hc._tracker_config(region)
    values = hc.read_sheet_values(region)
    if not values:
        return {"region": region, "error": "sheet unreadable", "ensure": ensure_result}
    idx = _header_index(values)
    at_claim_idx, current_idx = idx.get(ms.MARKETSHARE_AT_CLAIM_HEADER), idx.get(ms.MARKETSHARE_CURRENT_HEADER)
    if at_claim_idx is None or current_idx is None:
        return {"region": region, "error": "marketshare columns still missing after ensure_columns", "ensure": ensure_result}
    upc_idx, status_idx = idx.get(UPC_HEADER), idx.get(STATUS_HEADER)
    date_idx, email_idx, retracted_idx = idx.get(DATE_RECEIVED_HEADER), idx.get(EMAIL_STATUS_HEADER), idx.get(RETRACTED_HEADER)

    updates: List[tuple] = []   # (row_number, col_letter, value)
    backfilled = open_refreshed = skipped_no_date = 0
    open_rows: List[tuple] = []   # (row_number, upc)

    for row_number, row in enumerate(values[1:], start=2):
        upc = _cell(row, upc_idx)
        if not upc:
            continue
        status, email_status, retracted = _cell(row, status_idx), _cell(row, email_idx), _cell(row, retracted_idx)
        bucket = ms.bucket(status, email_status, retracted)

        if not _cell(row, at_claim_idx):
            received = _parse_date(_cell(row, date_idx))
            if not received:
                skipped_no_date += 1
            else:
                ppm = ms.get_marketshare_ppm_for_upc(upc, received)
                if ppm is not None:
                    updates.append((row_number, _col_letter(at_claim_idx), ppm))
                    backfilled += 1

        if bucket == "at_risk":
            open_rows.append((row_number, upc))

    if open_rows:
        ppm_by_upc = ms.get_marketshare_ppm_for_upcs([upc for _, upc in open_rows], today)
        for row_number, upc in open_rows:
            ppm = ppm_by_upc.get(upc)
            if ppm is not None:
                updates.append((row_number, _col_letter(current_idx), ppm))
                open_refreshed += 1

    if updates and not dry_run:
        value_ranges = [{"range": f"{sheet_id}!{col}{row}:{col}{row}", "values": [[val]]} for row, col, val in updates]
        from copyright_alert.lark_auth import sheet_values_batch_update

        sheet_values_batch_update(sheet_url, value_ranges)

    return {
        "region": region, "dry_run": dry_run, "ensure": ensure_result,
        "at_claim_backfilled": backfilled, "current_refreshed": open_refreshed,
        "skipped_no_date_received": skipped_no_date, "write_count": len(updates),
    }


def run_daily_sync_safe(region: str) -> Dict[str, object]:
    """Entry point for daily_workflow.py — never raises."""
    try:
        result = run_daily_sync(region)
        print(f"  ✓ Marketshare sync: {json.dumps(result, ensure_ascii=False)}", flush=True)
        return result
    except Exception as exc:
        print(f"  ✗ Marketshare sync failed (non-fatal): {exc!r}", flush=True)
        return {"error": repr(exc)}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sync marketshare-at-claim / marketshare-current tracker columns")
    parser.add_argument("--region", required=True, help="BR, US or SPLA")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_daily_sync(args.region.upper(), dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
