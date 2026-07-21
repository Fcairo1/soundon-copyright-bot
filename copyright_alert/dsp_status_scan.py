#!/usr/bin/env python3
"""Scheduled DSP delivery-status scan for the SoundOn tracker.

Reads the 2026 tab from the DSP status spreadsheet, checks delivery status for
rows that are not already complete, and writes status marks back to the DSP
status columns.

TikTok (column O) is intentionally ignored: we never use it to decide whether a
row is complete and never write to it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert.lark_auth import extract_sheet_values, sheet_values_api  # noqa: E402

DSP_STATUS_SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/HMJ2sV9q5h8BPIti79AlYyn5gNe"
DSP_STATUS_SHEET_ID = "2026"
DSP_STATUS_READ_RANGE = "A1:U5000"
DELIVERED_MARK = "✅"
NOT_SENT_MARK = "Not Sent"

# Column O is TikTok and must stay out of both read/filter logic and writes.
UPC_COLUMN = "J"
ISRC_COLUMN = "K"
TIKTOK_COLUMN = "O"
DSP_STATUS_COLUMNS: Dict[str, str] = {
    "spotify": "P",
    "facebook": "Q",
    "youtube": "R",
    "apple": "S",
    "soundcloud": "T",
    "deezer": "U",
}
DSP_STATUS_COLUMN_LETTERS: Tuple[str, ...] = tuple(DSP_STATUS_COLUMNS.values())

AEOLUS_BASE_URL = "https://aeolus-va.tiktok-row.net"
AEOLUS_SCRIPT = ROOT / "inner_skills" / "aeolus-platform-analysis" / "scripts" / "url_query.py"
ISRC_UPC_LOOKUP_URL = "https://aeolus-va.tiktok-row.net/pages/dataQuery?appId=1301&id=2469688856&sid=374690"
AUDIOSALAD_STATUS_URL = "https://aeolus-va.tiktok-row.net/pages/dataQuery?appId=5049&rid=5023707&sid=2935090"
ISRC_FILTER_FIELD = "isrc"
ISRC_UPC_RESULT_FIELD = "upc"
AUDIOSALAD_UPC_FILTER_FIELD = "upc"
AUDIOSALAD_TARGET_FIELD = "delivery_target_name"
AUDIOSALAD_STATUS_FIELD = "delivery_status"

DSP_TARGET_ALIASES: Dict[str, Tuple[str, ...]] = {
    "spotify": ("spotify",),
    "facebook": ("meta audio library", "facebook"),
    "youtube": ("youtube",),
    "apple": ("apple music", "apple music (direct)"),
    "soundcloud": ("soundcloud",),
    "deezer": ("deezer",),
}


def _col_index(letter: str) -> int:
    idx = 0
    for char in letter.upper():
        if not ("A" <= char <= "Z"):
            raise ValueError(f"Invalid column letter: {letter!r}")
        idx = idx * 26 + (ord(char) - ord("A") + 1)
    return idx - 1


def _cell(row: Sequence[object], col_letter: str) -> str:
    idx = _col_index(col_letter)
    if idx >= len(row):
        return ""
    value = row[idx]
    return "" if value is None else str(value).strip()


def _is_delivered(value: object) -> bool:
    return str(value or "").strip() == DELIVERED_MARK


def is_fully_delivered(row: Sequence[object]) -> bool:
    """Return True only when all non-TikTok DSP status columns are delivered.

    TikTok column O is deliberately excluded. A row with O unset but P:U all
    marked ✅ is complete and should not be queried again.
    """
    return all(_is_delivered(_cell(row, col)) for col in DSP_STATUS_COLUMN_LETTERS)


def read_rows_to_process(
    sheet_url: str = DSP_STATUS_SHEET_URL,
    sheet_id: str = DSP_STATUS_SHEET_ID,
    read_range: str = DSP_STATUS_READ_RANGE,
) -> Tuple[List[object], List[Tuple[int, List[object]]], int]:
    """Read sheet rows and return rows that still need DSP status work.

    Returns ``(header, rows_to_process, skipped_fully_delivered_count)`` where
    each row item is ``(sheet_row_number, row_values)``.
    """
    values = extract_sheet_values(sheet_values_api("GET", sheet_url, sheet_id, read_range))
    if not values:
        return [], [], 0

    header = list(values[0] or [])
    rows_to_process: List[Tuple[int, List[object]]] = []
    skipped_fully_delivered = 0

    for row_num, raw_row in enumerate(values[1:], start=2):
        row = list(raw_row or [])
        if not any(str(cell or "").strip() for cell in row):
            continue
        if is_fully_delivered(row):
            skipped_fully_delivered += 1
            continue
        rows_to_process.append((row_num, row))

    return header, rows_to_process, skipped_fully_delivered


def build_status_updates(row_num: int, statuses: Dict[str, object]) -> List[Tuple[str, str]]:
    """Build sheet cell updates for recognized non-TikTok DSP statuses only."""
    updates: List[Tuple[str, str]] = []
    for dsp_name, col_letter in DSP_STATUS_COLUMNS.items():
        if dsp_name not in statuses:
            continue
        if col_letter == TIKTOK_COLUMN:
            # Defensive guard: TikTok must never be written even if mappings are
            # edited later.
            continue
        value = statuses[dsp_name]
        updates.append((f"{col_letter}{row_num}", "" if value is None else str(value)))
    return updates


def write_status_updates(
    updates: Iterable[Tuple[str, str]],
    sheet_url: str = DSP_STATUS_SHEET_URL,
    sheet_id: str = DSP_STATUS_SHEET_ID,
) -> int:
    """Write single-cell DSP status updates, never touching TikTok column O."""
    count = 0
    for cell, value in updates:
        if cell.upper().startswith(TIKTOK_COLUMN):
            continue
        sheet_values_api("PUT", sheet_url, sheet_id, cell, values=[[value]])
        count += 1
    return count


def _parse_aeolus_json(stdout: str) -> Dict[str, object]:
    text = (stdout or "").strip()
    for marker in ("\n{", "{"):
        idx = text.rfind(marker)
        if idx >= 0:
            candidate = text[idx + (1 if marker.startswith("\n") else 0):]
            try:
                parsed = json.loads(candidate)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                continue
    return {}


def _query_aeolus_url(url: str, filters: Sequence[str], top_n: int = 100, timeout: int = 240) -> Dict[str, object]:
    if not AEOLUS_SCRIPT.exists():
        raise FileNotFoundError(f"Aeolus url_query script not found: {AEOLUS_SCRIPT}")
    cmd = [sys.executable, str(AEOLUS_SCRIPT), "--url", url, "--top-n", str(top_n)]
    for item in filters:
        cmd.extend(["--filters", item])
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(f"Aeolus query failed: {result.stderr.strip()[:500]}")
    return _parse_aeolus_json(result.stdout)


def lookup_upc_by_isrc(isrc: str) -> str:
    """Return UPC from the ISRC lookup DataQuery, or an empty string if absent."""
    value = str(isrc or "").strip()
    if not value:
        return ""
    payload = _query_aeolus_url(ISRC_UPC_LOOKUP_URL, [f"{ISRC_FILTER_FIELD}={value}"], top_n=5)
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        upc = str(row.get(ISRC_UPC_RESULT_FIELD) or "").strip()
        if upc and upc.upper() != "NULL":
            return upc
    return ""


def _is_ok_delivery(status: object) -> bool:
    return str(status or "").strip().lower() == "ok"


def query_audiosalad_statuses_by_upc(upc: str) -> Dict[str, str]:
    """Query the AudioSalad dashboard and map returned targets to tracker columns."""
    value = str(upc or "").strip()
    if not value:
        return {name: NOT_SENT_MARK for name in DSP_STATUS_COLUMNS}

    payload = _query_aeolus_url(
        AUDIOSALAD_STATUS_URL,
        [f"{AUDIOSALAD_UPC_FILTER_FIELD}={value}"],
        top_n=200,
    )
    delivered_targets = set()
    for row in payload.get("rows") or []:
        if not isinstance(row, dict) or not _is_ok_delivery(row.get(AUDIOSALAD_STATUS_FIELD)):
            continue
        target = str(row.get(AUDIOSALAD_TARGET_FIELD) or "").strip().lower()
        if target:
            delivered_targets.add(target)

    statuses: Dict[str, str] = {}
    for dsp_name, aliases in DSP_TARGET_ALIASES.items():
        matched = any(any(alias in target for alias in aliases) for target in delivered_targets)
        statuses[dsp_name] = DELIVERED_MARK if matched else NOT_SENT_MARK
    return statuses


def query_dsp_statuses(row: Sequence[object]) -> Dict[str, str]:
    """Query DSP delivery statuses for UPC rows and ISRC-only rows.

    Backward-compatible test hook: if DSP_STATUS_QUERY_MODULE is configured, it
    still delegates to that adapter. Otherwise, UPC comes from column J, or from
    the ISRC lookup dashboard when column J is empty and column K is present.
    """
    module_name = os.getenv("DSP_STATUS_QUERY_MODULE", "").strip()
    func_name = os.getenv("DSP_STATUS_QUERY_FUNC", "query_dsp_statuses").strip() or "query_dsp_statuses"
    if module_name:
        import importlib

        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
        result = func(row)
        if not isinstance(result, dict):
            raise TypeError(f"{module_name}.{func_name} returned {type(result).__name__}, expected dict")
        return {str(k).strip().lower(): str(v) for k, v in result.items() if k is not None}

    upc = _cell(row, UPC_COLUMN)
    if not upc:
        isrc = _cell(row, ISRC_COLUMN)
        upc = lookup_upc_by_isrc(isrc) if isrc else ""
    if not upc:
        return {name: NOT_SENT_MARK for name in DSP_STATUS_COLUMNS}
    return query_audiosalad_statuses_by_upc(upc)


def run(dry_run: bool = False) -> Dict[str, int]:
    _header, rows, skipped = read_rows_to_process()
    queried = 0
    written = 0
    upcs_filled = 0

    for row_num, row in rows:
        original_upc = _cell(row, UPC_COLUMN)
        updates: List[Tuple[str, str]] = []

        # If the row is ISRC-only, resolve UPC once, write it back to column J,
        # and reuse the resolved value for the normal AudioSalad status lookup.
        if not original_upc and _cell(row, ISRC_COLUMN):
            resolved_upc = lookup_upc_by_isrc(_cell(row, ISRC_COLUMN))
            if resolved_upc:
                j_idx = _col_index(UPC_COLUMN)
                if len(row) <= j_idx:
                    row.extend([""] * (j_idx + 1 - len(row)))
                row[j_idx] = resolved_upc
                updates.append((f"{UPC_COLUMN}{row_num}", resolved_upc))
                upcs_filled += 1

        statuses = query_dsp_statuses(row)
        queried += 1
        updates.extend(build_status_updates(row_num, statuses))

        if dry_run:
            written += len(updates)
            continue
        written += write_status_updates(updates)

    summary = {
        "rows_to_process": len(rows),
        "skipped_fully_delivered": skipped,
        "queried_rows": queried,
        "upcs_filled": upcs_filled,
        "written_cells": written,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan DSP delivery statuses without touching TikTok.")
    parser.add_argument("--dry-run", action="store_true", help="Read/filter rows and build updates without writing.")
    args = parser.parse_args(argv)
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
