#!/usr/bin/env python3
"""Backfill major-label claimant classification columns in regional trackers."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

from copyright_alert.major_label_detector import MAJOR_LABEL_HEADERS, classify_claimant, tracker_values
from copyright_alert.run_alert import _tracker_cell, parse_lark_annotated_csv

ROOT = Path(__file__).resolve().parents[2]


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


REGION_TRACKERS = {
    "BR": {
        "url": "https://bytedance.sg.larkoffice.com/sheets/HMQLsGgymhdIQ3tSbNNlk3m1gKd?sheet=c02dad",
        "sheet_id": "c02dad",
    },
    "SPLA": {
        "url": "https://bytedance.larkoffice.com/wiki/Ig1XwJc85iWmsGkEzujcy7sln9d?sheet=66eefc",
        "sheet_id": "66eefc",
    },
    "US": {
        "url": "https://bytedance.sg.larkoffice.com/sheets/FKqxsTu0bhl3ATt3n7YlIGvfgne",
        "sheet_id": "66eefc",
    },
}


def _run_lark(args: List[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["lark-cli", *args],
        input=input_text,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _read_rows(url: str, sheet_id: str, max_col: str = "AF") -> Tuple[List[str], List[List[str]], List[int]]:
    res = _run_lark([
        "sheets", "+csv-get", "--url", url, "--sheet-id", sheet_id,
        "--range", f"A1:{max_col}2000", "--max-chars", "500000",
    ])
    if res.returncode != 0:
        raise RuntimeError((res.stdout + res.stderr)[:1000])
    parsed, rows, row_numbers = parse_lark_annotated_csv(res.stdout)
    if not parsed or not rows:
        raise RuntimeError("Could not parse tracker rows")
    headers = [str(value or "").strip() for value in rows[0]]
    while headers and not headers[-1]:
        headers.pop()
    return headers, rows, row_numbers


def _ensure_columns(url: str, sheet_id: str, headers: List[str]) -> List[str]:
    missing = [header for header in MAJOR_LABEL_HEADERS if header not in headers]
    if not missing:
        return headers
    start_idx = len(headers)
    end_idx = start_idx + len(missing) - 1
    cell_range = f"{_col_letter(start_idx)}1:{_col_letter(end_idx)}1"
    cells = [[_tracker_cell(header) for header in missing]]
    res = _run_lark([
        "sheets", "+cells-set", "--url", url, "--sheet-id", sheet_id,
        "--range", cell_range, "--allow-overwrite=false", "--cells", "-",
    ], input_text=json.dumps(cells, ensure_ascii=False))
    if res.returncode != 0:
        raise RuntimeError((res.stdout + res.stderr)[:1000])
    return [*headers, *missing]


def _header_index(headers: List[str]) -> Dict[str, int]:
    return {header: idx for idx, header in enumerate(headers) if header}


def backfill_region(region: str, url: str, sheet_id: str) -> int:
    headers, rows, row_numbers = _read_rows(url, sheet_id)
    headers = _ensure_columns(url, sheet_id, headers)
    headers, rows, row_numbers = _read_rows(url, sheet_id)
    indices = _header_index(headers)

    required = ["Claimant", "Claimant Email", *MAJOR_LABEL_HEADERS]
    missing_required = [header for header in required if header not in indices]
    if missing_required:
        raise RuntimeError(f"{region}: missing required headers: {missing_required}")

    classification_indices = [indices[header] for header in MAJOR_LABEL_HEADERS]
    expected_indices = list(range(classification_indices[0], classification_indices[0] + len(MAJOR_LABEL_HEADERS)))
    if classification_indices != expected_indices:
        raise RuntimeError(f"{region}: classification columns are not contiguous/in expected order")

    row_numbers_by_offset = row_numbers or list(range(1, len(rows) + 1))
    row_by_number = {}
    max_row = 1
    updates = 0
    expected_cells = {}
    for offset, row in enumerate(rows[1:], start=1):
        row_number = row_numbers_by_offset[offset] if offset < len(row_numbers_by_offset) else offset + 1
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        row_by_number[row_number] = padded
        max_row = max(max_row, row_number)
        claimant_email = str(padded[indices["Claimant Email"]] or "").strip()
        claimant_group = str(padded[indices["Claimant Group"]] or "").strip()
        if not claimant_email or claimant_group:
            continue
        claimant_name = str(padded[indices["Claimant"]] or "").strip()
        values = tracker_values(classify_claimant(claimant_email, claimant_name))
        expected_cells[row_number] = [values[header] for header in MAJOR_LABEL_HEADERS]
        updates += 1

    if not updates:
        return 0

    start_col = _col_letter(classification_indices[0])
    end_col = _col_letter(classification_indices[-1])
    cell_range = f"{start_col}2:{end_col}{max_row}"
    cells = []
    for row_number in range(2, max_row + 1):
        if row_number in expected_cells:
            cells.append([_tracker_cell(value) for value in expected_cells[row_number]])
        else:
            cells.append([{}, {}, {}, {}])

    res = _run_lark([
        "sheets", "+cells-set", "--url", url, "--sheet-id", sheet_id,
        "--range", cell_range, "--cells", "-",
    ], input_text=json.dumps(cells, ensure_ascii=False))
    if res.returncode != 0:
        raise RuntimeError((res.stdout + res.stderr)[:1000])

    verify_headers, verify_rows, _verify_row_numbers = _read_rows(url, sheet_id)
    verify_indices = _header_index(verify_headers)
    verified = 0
    for row_number, expected in expected_cells.items():
        offset = row_number - 1
        if offset >= len(verify_rows):
            continue
        row = verify_rows[offset] + [""] * max(0, len(verify_headers) - len(verify_rows[offset]))
        actual = [str(row[verify_indices[header]] or "").strip() for header in MAJOR_LABEL_HEADERS]
        if actual == [str(value or "") for value in expected]:
            verified += 1
    if verified != updates:
        raise RuntimeError(f"{region}: verified {verified}/{updates} updated rows")
    return updates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", choices=["all", *REGION_TRACKERS.keys()], default="all")
    args = parser.parse_args()
    selected = REGION_TRACKERS if args.region == "all" else {args.region: REGION_TRACKERS[args.region]}
    results = {}
    for region, cfg in selected.items():
        results[region] = backfill_region(region, cfg["url"], cfg["sheet_id"])
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
