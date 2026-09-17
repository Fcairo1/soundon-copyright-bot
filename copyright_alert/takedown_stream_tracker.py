#!/usr/bin/env python3
"""Refresh the "ACR Takedown Stream Tracker" Lark sheet.

Answers: "how many Spotify streams did OUR original track get after we filed
the takedown report on the fraudulent copy?" acrcloud-dashboard appends a row
here the moment a case on the ACR tab is marked Takedown (so_song_id/so_isrc,
matched track, takedown_date) — it has no path to Aeolus itself (no internal
network access from Render). This script is the other half: it runs from
here (which does have Aeolus access) and:

  1. On a row's first pass (baseline_7d_streams blank), captures the
     original track's `api_sptf_play_cnt_7d` (real FUGA-sourced trailing
     7-day Spotify play count, confirmed via dataset 1576005 / Aeolus
     `report resolve` on the "Content ID streams" report) as the baseline.
  2. On later passes, re-queries the same metric and writes streams_delta_abs
     / streams_delta_pct as the change vs. that baseline (not vs. last
     week — the takedown-day baseline is the fixed reference point).

Self-throttled to roughly weekly per row via `last_checked`, rather than a
dedicated cron entry: intended to be called once from daily_workflow.py's
daily run (see run_daily_refresh()), same "reuse the existing schedule"
approach as account_release_counts.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import run_alert as ra  # noqa: E402
from copyright_alert.lark_auth import extract_sheet_values, sheet_values_api, sheet_values_batch_update  # noqa: E402

TAKEDOWN_STREAMS_SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/AbuUsJeSZhTkKQtpHBUlLyNhgNc"
TAKEDOWN_STREAMS_SHEET_ID = "75bc80"
DEFAULT_RANGE = "A1:M2000"

COLUMNS = [
    "so_song_id", "so_isrc", "song", "artist", "matched_title", "matched_isrc",
    "region", "takedown_date", "baseline_7d_streams", "latest_7d_streams",
    "streams_delta_abs", "streams_delta_pct", "last_checked",
]
COL_INDEX = {name: i for i, name in enumerate(COLUMNS)}
REFRESH_INTERVAL_DAYS = 7


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _cell(row: Sequence[object], idx: int) -> str:
    if idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()


def read_sheet_rows() -> List[Dict[str, str]]:
    payload = sheet_values_api("GET", TAKEDOWN_STREAMS_SHEET_URL, TAKEDOWN_STREAMS_SHEET_ID, DEFAULT_RANGE)
    values = extract_sheet_values(payload)
    rows = []
    for row_number, raw in enumerate(values[1:], start=2):
        entry = {name: _cell(raw, idx) for name, idx in COL_INDEX.items()}
        if entry["so_isrc"] or entry["so_song_id"]:
            entry["_row_number"] = row_number
            rows.append(entry)
    return rows


def _needs_refresh(entry: Dict[str, str], *, now: date) -> bool:
    if not entry.get("baseline_7d_streams"):
        return True
    last_checked = entry.get("last_checked", "")
    if not last_checked:
        return True
    try:
        checked_date = datetime.strptime(last_checked[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    return (now - checked_date) >= timedelta(days=REFRESH_INTERVAL_DAYS)


def batch_query_sptf_7d_by_isrc(isrcs: Sequence[str], *, p_date: str, chunk_size: int = 40) -> Dict[str, int]:
    """Return {isrc: api_sptf_play_cnt_7d} for the given ISRCs at one partition.

    Same table/dataset as run_alert.py's batch_query_engagement_by_upc
    (ENGAGEMENT_TABLE / ENGAGEMENT_DATASET = 1576005, the "Content ID
    streams" report) but keyed by isrc and pulling the real (non-approximate)
    api_sptf_play_cnt_7d metric instead of the 30d one already used there.
    """
    unique = []
    seen = set()
    for isrc in isrcs:
        value = str(isrc or "").strip()
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    if not unique:
        return {}

    results: Dict[str, int] = {}
    for idx in range(0, len(unique), chunk_size):
        chunk = unique[idx:idx + chunk_size]
        in_list = ", ".join(f"'{ra._aeolus_sql_quote(v)}'" for v in chunk)
        sql = (
            "SELECT `[isrc]` AS isrc, SUM(`[api_sptf_play_cnt_7d]`) AS sptf_7d_str "
            f"FROM `{ra.ENGAGEMENT_TABLE}` "
            f"WHERE `[isrc]` IN ({in_list}) AND `[p_date]` = '{p_date}' "
            "GROUP BY `[isrc]`"
        )
        try:
            parsed = ra._run_aeolus_sql(sql, timeout=240, dataset_id=ra.ENGAGEMENT_DATASET)
        except Exception as exc:
            print(f"  ✗ Takedown-stream query failed for chunk starting at {idx}: {exc}")
            continue
        if not parsed:
            print(f"  ✗ Could not parse Aeolus JSON for chunk starting at {idx}")
            continue
        for row in ra._aeolus_rows_to_dict(parsed):
            isrc = str(row.get("isrc") or "").strip()
            if not isrc:
                continue
            value = row.get("sptf_7d_str")
            try:
                results[isrc] = int(float(value)) if value not in (None, "") else 0
            except (TypeError, ValueError):
                results[isrc] = 0
    return results


def _pct_delta(baseline: int, latest: int) -> float:
    if baseline > 0:
        return round((latest - baseline) / baseline * 100.0, 1)
    return 0.0 if latest == 0 else 100.0


def plan_updates(rows: List[Dict[str, str]], streams_by_isrc: Dict[str, int], *, now: Optional[datetime] = None) -> List[Dict[str, object]]:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    for entry in rows:
        isrc = entry["so_isrc"]
        if isrc not in streams_by_isrc:
            continue
        latest = streams_by_isrc[isrc]
        row_number = entry["_row_number"]
        baseline_raw = entry.get("baseline_7d_streams", "")
        if not baseline_raw:
            baseline = latest
            delta_abs, delta_pct = 0, 0.0
        else:
            try:
                baseline = int(float(baseline_raw))
            except ValueError:
                baseline = latest
            delta_abs = latest - baseline
            delta_pct = _pct_delta(baseline, latest)
        values = {
            "baseline_7d_streams": baseline,
            "latest_7d_streams": latest,
            "streams_delta_abs": delta_abs,
            "streams_delta_pct": delta_pct,
            "last_checked": timestamp,
        }
        for field, value in values.items():
            col = _column_letter(COL_INDEX[field])
            updates.append({
                "range": f"{TAKEDOWN_STREAMS_SHEET_ID}!{col}{row_number}:{col}{row_number}",
                "values": [[value]],
            })
    return updates


def run(*, dry_run: bool = False, now: Optional[datetime] = None) -> Dict[str, object]:
    now = now or datetime.now()
    rows = read_sheet_rows()
    due = [r for r in rows if r["so_isrc"] and _needs_refresh(r, now=now.date())]
    if not due:
        return {"dry_run": dry_run, "tracked_rows": len(rows), "due_for_refresh": 0, "updated": 0}

    p_date = ra._resolve_engagement_partition()
    streams_by_isrc = batch_query_sptf_7d_by_isrc([r["so_isrc"] for r in due], p_date=p_date)
    updates = plan_updates(due, streams_by_isrc, now=now)
    if not dry_run and updates:
        sheet_values_batch_update(TAKEDOWN_STREAMS_SHEET_URL, updates)
    return {
        "dry_run": dry_run,
        "p_date": p_date,
        "tracked_rows": len(rows),
        "due_for_refresh": len(due),
        "isrcs_found_in_aeolus": len(streams_by_isrc),
        "write_count": len(updates),
    }


def run_daily_refresh() -> Dict[str, object]:
    """Entry point for daily_workflow.py — swallows errors so a stream-tracker
    hiccup never breaks the rest of the daily run."""
    try:
        result = run()
        print(f"  ✓ Takedown stream tracker: {json.dumps(result, ensure_ascii=False)}")
        return result
    except Exception as exc:
        print(f"  ✗ Takedown stream tracker failed (non-fatal): {exc}")
        return {"error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the ACR Takedown Stream Tracker sheet from Aeolus")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to the sheet")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
