#!/usr/bin/env python3
"""Keep the Account Release Counts sheet fresh.

Automation rules:
- Account scope comes from infringement tracker rows whose Date Received is in the
  current calendar quarter, then is cross-checked in Aeolus as BR + AP/A&R.
- Existing UIDs only refresh the current quarter column and last_updated.
- New UIDs receive uid, user_name, last_updated, total_releases and the current
  quarter value.
- Past quarter columns are never rewritten or blanked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import run_alert as ra  # noqa: E402
from copyright_alert.lark_auth import extract_sheet_values, sheet_values_api, sheet_values_batch_update  # noqa: E402
from copyright_alert.paths import inner_skill  # noqa: E402

BRT = ZoneInfo("America/Sao_Paulo")
ACCOUNT_RELEASE_COUNTS_SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/ORsCs7cKnhjOtQtBYmDlTBsogtg"
ACCOUNT_RELEASE_COUNTS_SHEET_ID_ENV = "ACCOUNT_RELEASE_COUNTS_SHEET_ID"
RELEASE_COUNTS_SHEET_ID_ENV = "RELEASE_COUNTS_SHEET_ID"
ACCOUNT_RELEASE_COUNTS_RANGE_ENV = "ACCOUNT_RELEASE_COUNTS_RANGE"
ACCOUNT_RELEASE_COUNTS_P_DATE_ENV = "ACCOUNT_RELEASE_COUNTS_P_DATE"
DEFAULT_ACCOUNT_RELEASE_COUNTS_RANGE = "A1:ZZ2000"
SONG_DIMENSION_DATASET_ID = "374690"
SONG_DIMENSION_TABLE = "aeolus_data_db_aeolus_my_upsilon_202606.aeolus_data_table_2_3007307_prod"
AEOLUS_BASE_URL = "https://aeolus-va.tiktok-row.net"

TRACKERS = [
    {
        "name": "BR",
        "url": "https://bytedance.sg.larkoffice.com/sheets/HMQLsGgymhdIQ3tSbNNlk3m1gKd?sheet=c02dad",
        "sheet_id": "c02dad",
    },
    {
        "name": "SPLA",
        "url": "https://bytedance.larkoffice.com/wiki/Ig1XwJc85iWmsGkEzujcy7sln9d?sheet=66eefc",
        "sheet_id": "",
    },
    {
        "name": "US",
        "url": "https://bytedance.sg.larkoffice.com/sheets/FKqxsTu0bhl3ATt3n7YlIGvfgne",
        "sheet_id": "",
    },
]

FIXED_HEADERS = ["uid", "user_name", "last_updated", "total_releases"]
UID_HEADER_ALIASES = {"uid", "user id", "user_id", "label uid", "account uid"}
DATE_HEADER_ALIASES = {"date received", "date_received", "detected at", "received date"}


def current_quarter(now: Optional[datetime] = None) -> Dict[str, str]:
    now = now.astimezone(BRT) if now else datetime.now(BRT)
    quarter = (now.month - 1) // 3 + 1
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 3
    end_year = now.year
    if end_month > 12:
        end_year += 1
        end_month -= 12
    return {
        "label": f"Q{quarter} {now.year}",
        "start": f"{now.year}-{start_month:02d}-01",
        "end_exclusive": f"{end_year}-{end_month:02d}-01",
    }


def _normalize_uid(uid) -> str:
    value = str(uid or "").strip()
    if value.endswith(".0") and value[:-2].isdigit():
        return value[:-2]
    return value


def _safe_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> Optional[date]:
    text = str(value or "").strip()
    if not text or text.upper() == "N/A":
        return None
    text = text.replace("/", "-")
    match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.search(r"(\d{1,2})-(\d{1,2})-(20\d{2})", text)
    if match:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    return None


def _header_index(headers: Sequence[str], aliases: set[str]) -> Optional[int]:
    for idx, header in enumerate(headers):
        normalized = " ".join(str(header or "").strip().lower().replace("_", " ").split())
        if normalized in aliases or normalized.replace(" ", "_") in aliases:
            return idx
    return None


def _cell(row: Sequence[object], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()


def _sheet_id_from_env() -> Optional[str]:
    return os.getenv(ACCOUNT_RELEASE_COUNTS_SHEET_ID_ENV, "").strip() or os.getenv(RELEASE_COUNTS_SHEET_ID_ENV, "").strip() or None


def resolve_first_sheet_id(sheet_url: str, *, use_env: bool = True) -> str:
    explicit = _sheet_id_from_env() if use_env else None
    if explicit:
        return explicit
    result = subprocess.run(
        ["lark-cli", "sheets", "+workbook-info", "--url", sheet_url],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr)[:1000])
    payload = json.loads(result.stdout[result.stdout.find("{") :])
    sheets = (((payload.get("data") or {}).get("sheets")) or payload.get("sheets") or [])
    if not sheets:
        raise RuntimeError("Could not resolve any sheet_id from workbook-info")
    return str(sheets[0].get("sheet_id") or sheets[0].get("sheetId") or sheets[0].get("id"))


def read_sheet_values(sheet_url: str, sheet_id: str, cell_range: str = DEFAULT_ACCOUNT_RELEASE_COUNTS_RANGE) -> List[list]:
    return extract_sheet_values(sheet_values_api("GET", sheet_url, sheet_id, cell_range))


def _sheet_param_from_url(sheet_url: str) -> Optional[str]:
    parsed = urllib.parse.urlparse(sheet_url)
    values = urllib.parse.parse_qs(parsed.query).get("sheet") or []
    return values[0] if values else None


def read_tracker_values(tracker: Dict[str, str]) -> List[list]:
    sheet_id = tracker.get("sheet_id") or _sheet_param_from_url(tracker["url"]) or resolve_first_sheet_id(tracker["url"], use_env=False)
    return read_sheet_values(tracker["url"], sheet_id, "A1:AF2000")


def discover_claim_uids_from_trackers(*, quarter: Optional[Dict[str, str]] = None, region: Optional[str] = None) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    quarter = quarter or current_quarter()
    start = datetime.fromisoformat(quarter["start"]).date()
    end = datetime.fromisoformat(quarter["end_exclusive"]).date()
    discovered: Dict[str, Dict[str, str]] = {}
    warnings = []
    for tracker in TRACKERS:
        if region and tracker["name"].upper() != region.upper():
            continue
        try:
            values = read_tracker_values(tracker)
        except Exception as exc:
            warning = f"{tracker['name']} tracker could not be read and was skipped: {exc}"
            print(f"⚠ {warning}")
            warnings.append(warning)
            continue
        if not values:
            continue
        headers = [str(value or "").strip() for value in values[0]]
        uid_idx = _header_index(headers, UID_HEADER_ALIASES)
        date_idx = _header_index(headers, DATE_HEADER_ALIASES)
        if uid_idx is None or date_idx is None:
            warning = f"{tracker['name']} tracker missing UID or Date Received header; skipped"
            print(f"⚠ {warning}")
            warnings.append(warning)
            continue
        for row in values[1:]:
            uid = _normalize_uid(_cell(row, uid_idx))
            received = _parse_date(_cell(row, date_idx))
            if not uid or uid.upper() == "N/A" or not received or not (start <= received < end):
                continue
            discovered.setdefault(uid, {"uid": uid, "tracker": tracker["name"], "date_received": received.isoformat()})
    return discovered, warnings


def latest_p_date() -> str:
    override = os.getenv(ACCOUNT_RELEASE_COUNTS_P_DATE_ENV, "").strip()
    if override:
        return override
    script = inner_skill("aeolus-platform-analysis", "scripts", "dataset_model.py")
    result = subprocess.run(
        ["python3", str(script), "--dataset-id", SONG_DIMENSION_DATASET_ID, "--base-url", AEOLUS_BASE_URL],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr)[:1000])
    payload = json.loads(result.stdout[result.stdout.find("{") :])
    for item in payload.get("partitionInfo") or []:
        if item.get("name") == "p_date":
            values = [str(value) for value in item.get("valueList") or [] if value]
            if values:
                return max(values)
    partition_range = ((payload.get("overview") or {}).get("partitionRange") or [])
    for item in partition_range:
        if item.get("name") == "p_date" and item.get("max"):
            return str(item["max"])
    raise RuntimeError("Could not determine latest p_date for dataset 374690")


def _run_song_dimension_sql(sql: str, *, timeout: int = 240) -> Optional[dict]:
    old_base = getattr(ra, "AEOLUS_BASE", None)
    ra.AEOLUS_BASE = AEOLUS_BASE_URL
    try:
        return ra._run_aeolus_sql(sql, timeout=timeout, dataset_id=SONG_DIMENSION_DATASET_ID)
    finally:
        if old_base is not None:
            ra.AEOLUS_BASE = old_base


def _sql_string(value: str) -> str:
    return "'" + ra._aeolus_sql_quote(value) + "'"


def _latest_partition_clause(p_date: str) -> str:
    return f"p_date = {_sql_string(p_date)}"


def _base_scope_clause(target_region: Optional[str] = None) -> str:
    region_filter = "region = 'BR'"
    if target_region == "SPLA":
        region_filter = "region IN ('MX','CL','CO','AR','ES','PR','PE')"
    elif target_region == "US":
        region_filter = "region IN ('US','CA','AU','NZ')"
    elif target_region == "BR":
        region_filter = "region = 'BR'"
    elif target_region:
        from copyright_alert import run_alert as ra
        region_filter = f"region = '{ra._aeolus_sql_quote(target_region)}'"
    return f"{region_filter} AND `source_type[dim_user]` IN (5, 6) AND status_desc != 'REJECT'"


def query_account_release_counts_many(
    uids: Iterable[str],
    *,
    quarter: Optional[Dict[str, str]] = None,
    include_total_for: Optional[set[str]] = None,
    p_date: Optional[str] = None,
    region: Optional[str] = None,
) -> List[Dict[str, object]]:
    quarter = quarter or current_quarter()
    unique_uids = []
    seen = set()
    for uid in uids:
        value = _normalize_uid(uid)
        if value and value.upper() != "N/A" and value not in seen:
            seen.add(value)
            unique_uids.append(value)
    if not unique_uids:
        return []
    include_total_for = include_total_for or set(unique_uids)
    p_date = p_date or latest_p_date()
    in_list = ", ".join(_sql_string(uid) for uid in unique_uids)
    total_expr = "COUNT(DISTINCT album_id) AS total_releases" if include_total_for else "CAST(NULL AS Nullable(UInt64)) AS total_releases"
    sql = f"""
SELECT
  user_id AS uid,
  any(user_name) AS user_name,
  {total_expr},
  COUNT(DISTINCT CASE
    WHEN toDate(release_time) >= toDate('{ra._aeolus_sql_quote(quarter['start'])}')
     AND toDate(release_time) < toDate('{ra._aeolus_sql_quote(quarter['end_exclusive'])}')
    THEN album_id
  END) AS current_quarter_releases
FROM {SONG_DIMENSION_TABLE}
WHERE user_id IN ({in_list})
  AND {_latest_partition_clause(p_date)}
  AND {_base_scope_clause(region)}
GROUP BY user_id
""".strip()
    parsed = _run_song_dimension_sql(sql, timeout=300)
    rows = ra._aeolus_rows_to_dict(parsed) if parsed else []
    by_uid = {_normalize_uid(row.get("uid")): row for row in rows}
    out = []
    for uid in unique_uids:
        row = by_uid.get(uid)
        if not row:
            continue
        result = {
            "uid": uid,
            "user_name": str(row.get("user_name") or "").strip(),
            "current_quarter_releases": _safe_int(row.get("current_quarter_releases")) or 0,
            "quarter_label": quarter["label"],
        }
        if uid in include_total_for:
            result["total_releases"] = _safe_int(row.get("total_releases")) or 0
        out.append(result)
    return out


def query_account_release_counts(uid: str, *, quarter: Optional[Dict[str, str]] = None, p_date: Optional[str] = None) -> Dict[str, object]:
    rows = query_account_release_counts_many([uid], quarter=quarter, include_total_for={_normalize_uid(uid)}, p_date=p_date)
    return rows[0] if rows else {}


def inspect_account_release_sheet(values: List[list], quarter_label: str) -> Dict[str, object]:
    headers = [str(value or "").strip() for value in (values[0] if values else [])]
    existing = {}
    for row_number, row in enumerate(values[1:], start=2):
        uid = _normalize_uid(_cell(row, 0))
        if uid:
            existing[uid] = row_number
    first_empty = len(values) + 1 if values else 2
    for row_number, row in enumerate(values[1:], start=2):
        if not any(_cell(row, idx) for idx in range(max(len(headers), 4))):
            first_empty = row_number
            break
    quarter_idx = None
    for idx, header in enumerate(headers):
        if header == quarter_label:
            quarter_idx = idx
            break
    return {"headers": headers, "existing": existing, "first_empty_row": first_empty, "quarter_index": quarter_idx}


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _last_updated(now: Optional[datetime] = None) -> str:
    now = now.astimezone(BRT) if now else datetime.now(BRT)
    return now.strftime("%Y-%m-%d %H:%M:%S BRT")


def plan_sheet_updates(
    counts_rows: Sequence[Dict[str, object]],
    sheet_values: List[list],
    sheet_id: str,
    *,
    quarter_label: str,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    inspected = inspect_account_release_sheet(sheet_values, quarter_label)
    headers = list(inspected["headers"])
    if not headers:
        headers = FIXED_HEADERS[:]
    for idx, expected in enumerate(FIXED_HEADERS):
        if idx >= len(headers):
            headers.append(expected)
        elif not headers[idx]:
            headers[idx] = expected
    quarter_index = inspected["quarter_index"]
    header_update = None
    if quarter_index is None:
        quarter_index = len(headers)
        headers.append(quarter_label)
        cell = f"{sheet_id}!{_column_letter(quarter_index)}1:{_column_letter(quarter_index)}1"
        header_update = {"range": cell, "values": [[quarter_label]]}
    updates = []
    actions = []
    next_row = int(inspected["first_empty_row"])
    existing = inspected["existing"]
    timestamp = _last_updated(now)
    for counts in counts_rows:
        uid = _normalize_uid(counts.get("uid"))
        if not uid:
            continue
        row_number = existing.get(uid)
        is_new = row_number is None
        if is_new:
            row_number = next_row
            next_row += 1
            updates.extend([
                {"range": f"{sheet_id}!A{row_number}:A{row_number}", "values": [[uid]]},
                {"range": f"{sheet_id}!B{row_number}:B{row_number}", "values": [[str(counts.get('user_name') or '')]]},
                {"range": f"{sheet_id}!C{row_number}:C{row_number}", "values": [[timestamp]]},
                {"range": f"{sheet_id}!D{row_number}:D{row_number}", "values": [[str(counts.get('total_releases', ''))]]},
            ])
            action = "insert"
        else:
            updates.append({"range": f"{sheet_id}!C{row_number}:C{row_number}", "values": [[timestamp]]})
            action = "update_current_quarter"
        q_col = _column_letter(int(quarter_index))
        updates.append({"range": f"{sheet_id}!{q_col}{row_number}:{q_col}{row_number}", "values": [[str(counts.get('current_quarter_releases', 0))]]})
        actions.append({"action": action, "row_number": row_number, "uid": uid, "user_name": counts.get("user_name", ""), quarter_label: counts.get("current_quarter_releases", 0), "total_releases": counts.get("total_releases")})
    if header_update:
        updates.insert(0, header_update)
    return {"updates": updates, "actions": actions, "quarter_column_created": bool(header_update), "quarter_index": quarter_index}


def run(*, dry_run: bool = False, sheet_url: str = ACCOUNT_RELEASE_COUNTS_SHEET_URL, sheet_id: Optional[str] = None, now: Optional[datetime] = None, region: Optional[str] = None) -> Dict[str, object]:
    quarter = current_quarter(now)
    sheet_id = sheet_id or resolve_first_sheet_id(sheet_url)
    account_values = read_sheet_values(sheet_url, sheet_id, os.getenv(ACCOUNT_RELEASE_COUNTS_RANGE_ENV, DEFAULT_ACCOUNT_RELEASE_COUNTS_RANGE))
    inspected = inspect_account_release_sheet(account_values, quarter["label"])
    discovered, warnings = discover_claim_uids_from_trackers(quarter=quarter, region=region)
    existing_uids = set(inspected["existing"].keys())
    new_uids = set(discovered.keys()) - existing_uids
    p_date = latest_p_date()
    counts_rows = query_account_release_counts_many(discovered.keys(), quarter=quarter, include_total_for=new_uids, p_date=p_date, region=region)
    plan = plan_sheet_updates(counts_rows, account_values, sheet_id, quarter_label=quarter["label"], now=now)
    if not dry_run and plan["updates"]:
        sheet_values_batch_update(sheet_url, plan["updates"])
    return {
        "dry_run": dry_run,
        "sheet_url": sheet_url,
        "sheet_id": sheet_id,
        "quarter": quarter,
        "p_date": p_date,
        "candidate_uids_from_trackers": len(discovered),
        "aeolus_scoped_uids": len(counts_rows),
        "new_uids": sorted(new_uids),
        "existing_uids_refreshed": sorted(set(row["uid"] for row in counts_rows) - new_uids),
        "quarter_column_created": plan["quarter_column_created"],
        "write_count": len(plan["updates"]),
        "warnings": warnings,
        "actions": plan["actions"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Account Release Counts from infringement trackers + Aeolus")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to the sheet")
    parser.add_argument("--region", default=None, help="Target region (BR, SPLA, US)")
    parser.add_argument("--sheet-url", default=ACCOUNT_RELEASE_COUNTS_SHEET_URL)
    parser.add_argument("--sheet-id", default=None)
    parser.add_argument("--as-of", default=None, help="Optional YYYY-MM-DD date for deterministic quarter selection")
    args = parser.parse_args()
    now = None
    if args.as_of:
        now = datetime.fromisoformat(args.as_of).replace(tzinfo=BRT)
    result = run(dry_run=args.dry_run, sheet_url=args.sheet_url, sheet_id=args.sheet_id, now=now, region=args.region)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
