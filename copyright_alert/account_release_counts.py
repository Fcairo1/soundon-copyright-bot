#!/usr/bin/env python3
"""Account-level release counts for infringement claim accounts.

This module is intentionally safe by default: the dry-run path only prints rows
that would be written, while the write path requires RELEASE_COUNTS_SHEET_TOKEN
(or RELEASE_COUNTS_SHEET_URL) plus RELEASE_COUNTS_SHEET_ID.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import run_alert as ra  # noqa: E402
from copyright_alert.lark_auth import extract_sheet_values, sheet_values_api  # noqa: E402

BRT = ZoneInfo("America/Sao_Paulo")
RELEASE_COUNTS_SHEET_TOKEN_ENV = "RELEASE_COUNTS_SHEET_TOKEN"
RELEASE_COUNTS_SHEET_URL_ENV = "RELEASE_COUNTS_SHEET_URL"
RELEASE_COUNTS_SHEET_ID_ENV = "RELEASE_COUNTS_SHEET_ID"
RELEASE_COUNTS_RANGE_ENV = "RELEASE_COUNTS_RANGE"
RELEASE_COUNTS_P_DATE_START_ENV = "RELEASE_COUNTS_P_DATE_START"
DEFAULT_RELEASE_COUNTS_RANGE = "A:F"
DEFAULT_P_DATE_START = "2020-01-01"
COLUMNS = ["uid", "user_name", "total_releases", "releases_this_quarter", "quarter_label", "last_updated"]


def current_quarter(now: Optional[datetime] = None) -> Dict[str, str]:
    """Return the current quarter boundaries using Brazil time as source of truth."""
    now = now.astimezone(BRT) if now else datetime.now(BRT)
    quarter = (now.month - 1) // 3 + 1
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 3
    end_year = now.year
    if end_month > 12:
        end_year += 1
        end_month -= 12
    return {
        "label": f"{now.year}-Q{quarter}",
        "start": f"{now.year}-{start_month:02d}-01",
        "end_exclusive": f"{end_year}-{end_month:02d}-01",
    }


def _normalize_uid(uid) -> str:
    return str(uid or "").strip()


def _safe_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def query_account_release_counts(uid: str, *, quarter: Optional[Dict[str, str]] = None) -> Dict[str, object]:
    """Return active/approved release counts for one SoundOn account UID.

    Counts use the same Aeolus Song Dimension dataset as run_alert.query_aeolus().
    total_releases is all-time; releases_this_quarter is constrained to the
    supplied/current quarter using release_time_format.
    """
    uid = _normalize_uid(uid)
    if not uid or uid == "N/A":
        return {}
    quarter = quarter or current_quarter()
    p_date_start = os.getenv(RELEASE_COUNTS_P_DATE_START_ENV, DEFAULT_P_DATE_START).strip() or DEFAULT_P_DATE_START
    sql = (
        "SELECT `[user_id]` AS uid, any(`[user_name]`) AS user_name, "
        "COUNT(DISTINCT `[album_id]`) AS total_releases, "
        "COUNT(DISTINCT CASE WHEN `[release_time_format]` >= '{start}' "
        "AND `[release_time_format]` < '{end}' THEN `[album_id]` END) AS releases_this_quarter "
        "FROM `[[AOP] Song Dimension]` "
        "WHERE `[user_id]` = '{uid}' AND `[album_status]` = 2 "
        "AND `[p_date]` >= '{p_date_start}' "
        "GROUP BY `[user_id]`"
    ).format(
        uid=ra._aeolus_sql_quote(uid),
        start=ra._aeolus_sql_quote(quarter["start"]),
        end=ra._aeolus_sql_quote(quarter["end_exclusive"]),
        p_date_start=ra._aeolus_sql_quote(p_date_start),
    )
    parsed = ra._run_aeolus_sql(sql, timeout=240)
    if not parsed or str(parsed.get("code", "aeolus/ok")) not in ("0", "", "aeolus/ok"):
        print(f"  ✗ Could not query Aeolus release counts for UID {uid}")
        return {}
    rows = ra._aeolus_rows_to_dict(parsed)
    if not rows:
        return {
            "uid": uid,
            "user_name": "",
            "total_releases": 0,
            "releases_this_quarter": 0,
            "quarter_label": quarter["label"],
        }
    row = rows[0]
    return {
        "uid": _normalize_uid(row.get("uid") or uid),
        "user_name": str(row.get("user_name") or "").strip(),
        "total_releases": _safe_int(row.get("total_releases")),
        "releases_this_quarter": _safe_int(row.get("releases_this_quarter")),
        "quarter_label": quarter["label"],
    }


def query_account_release_counts_many(uids: Iterable[str], *, quarter: Optional[Dict[str, str]] = None) -> List[Dict[str, object]]:
    rows = []
    seen = set()
    for uid in uids:
        value = _normalize_uid(uid)
        if not value or value == "N/A" or value in seen:
            continue
        seen.add(value)
        rows.append(query_account_release_counts(value, quarter=quarter))
    return rows


def build_release_count_sheet_row(counts: Dict[str, object], *, now: Optional[datetime] = None) -> Dict[str, object]:
    now = now.astimezone(BRT) if now else datetime.now(BRT)
    row = {key: counts.get(key) for key in COLUMNS if key in counts and counts.get(key) is not None}
    row["last_updated"] = now.strftime("%Y-%m-%d %H:%M:%S BRT")
    return row


def _sheet_url() -> str:
    value = os.getenv(RELEASE_COUNTS_SHEET_URL_ENV, "").strip() or os.getenv(RELEASE_COUNTS_SHEET_TOKEN_ENV, "").strip()
    if not value:
        raise RuntimeError(f"Set {RELEASE_COUNTS_SHEET_TOKEN_ENV} or {RELEASE_COUNTS_SHEET_URL_ENV} before writing release counts")
    return value


def _sheet_id() -> str:
    value = os.getenv(RELEASE_COUNTS_SHEET_ID_ENV, "").strip()
    if not value:
        raise RuntimeError(f"Set {RELEASE_COUNTS_SHEET_ID_ENV} before writing release counts")
    return value


def _cell(row: list, idx: int) -> str:
    if idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()


def read_release_count_sheet(sheet_url: str, sheet_id: str, cell_range: str = DEFAULT_RELEASE_COUNTS_RANGE) -> List[list]:
    payload = sheet_values_api("GET", sheet_url, sheet_id, cell_range)
    return extract_sheet_values(payload)


def _row_has_values(row: list) -> bool:
    return any(_cell(row, idx) for idx in range(len(COLUMNS)))


def inspect_release_count_rows(values: List[list]) -> Dict[str, object]:
    existing = {}
    first_empty = None
    for row_number, row in enumerate(values[1:], start=2):
        uid = _cell(row, 0)
        if uid:
            existing[uid] = row_number
        if first_empty is None and not _row_has_values(row):
            first_empty = row_number
    if first_empty is None:
        first_empty = len(values) + 1 if values else 2
    return {"existing": existing, "first_empty_row": first_empty}


def read_existing_release_count_rows(sheet_url: str, sheet_id: str, cell_range: str = DEFAULT_RELEASE_COUNTS_RANGE) -> Dict[str, int]:
    return inspect_release_count_rows(read_release_count_sheet(sheet_url, sheet_id, cell_range))["existing"]


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def build_partial_update_ranges(sheet_id: str, row_number: int, row: Dict[str, object]) -> List[Dict[str, object]]:
    ranges = []
    for col_index, column in enumerate(COLUMNS):
        if column not in row or row[column] is None:
            continue
        cell = f"{sheet_id}!{_column_letter(col_index)}{row_number}:{_column_letter(col_index)}{row_number}"
        ranges.append({"range": cell, "values": [[str(row[column])]]})
    return ranges


def upsert_release_count_row(counts: Dict[str, object], *, dry_run: bool = True) -> Dict[str, object]:
    row = build_release_count_sheet_row(counts)
    uid = _normalize_uid(row.get("uid"))
    if not uid:
        raise ValueError("release-count row is missing uid")
    if dry_run:
        return {"dry_run": True, "row": row}

    sheet_url = _sheet_url()
    sheet_id = _sheet_id()
    cell_range = os.getenv(RELEASE_COUNTS_RANGE_ENV, DEFAULT_RELEASE_COUNTS_RANGE).strip() or DEFAULT_RELEASE_COUNTS_RANGE
    inspected = inspect_release_count_rows(read_release_count_sheet(sheet_url, sheet_id, cell_range))
    existing = inspected["existing"]
    if uid in existing:
        for value_range in build_partial_update_ranges(sheet_id, existing[uid], row):
            sheet_values_api("PUT", sheet_url, sheet_id, value_range["range"].split("!", 1)[1], values=value_range["values"])
        return {"dry_run": False, "action": "update", "row_number": existing[uid], "row": row}

    next_row = inspected["first_empty_row"]
    values = [[str(row.get(column, "")) for column in COLUMNS]]
    sheet_values_api("PUT", sheet_url, sheet_id, f"A{next_row}:F{next_row}", values=values)
    return {"dry_run": False, "action": "insert", "row_number": next_row, "row": row}


def dry_run_for_uids(uids: Iterable[str], *, quarter: Optional[Dict[str, str]] = None) -> List[Dict[str, object]]:
    results = []
    for counts in query_account_release_counts_many(uids, quarter=quarter):
        results.append(upsert_release_count_row(counts, dry_run=True)["row"])
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run or write account release-count rows")
    parser.add_argument("uids", nargs="+", help="SoundOn account UID(s)")
    parser.add_argument("--write", action="store_true", help="Actually write to RELEASE_COUNTS_SHEET_* target")
    args = parser.parse_args()
    rows = []
    for counts in query_account_release_counts_many(args.uids):
        rows.append(upsert_release_count_row(counts, dry_run=not args.write))
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
