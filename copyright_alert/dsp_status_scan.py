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

from copyright_alert.lark_auth import extract_sheet_values, sheet_values_api, sheet_values_batch_update  # noqa: E402
from copyright_alert.paths import inner_skill  # noqa: E402

DSP_STATUS_SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/HMJ2sV9q5h8BPIti79AlYyn5gNe"
DSP_STATUS_SHEET_ID = "XFzPWy"
DSP_STATUS_READ_RANGE = "A1:U5000"
# The DSP status sheet has TWO frozen header rows: row 1 is a merged title
# banner and row 2 holds the column labels (J2="UPC", K2="ISRC", P2="Spotify",
# ...). Actual data begins at row 3.
HEADER_ROW_COUNT = 2
UPC_HEADER_LABEL = "UPC"
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
AEOLUS_SCRIPT = inner_skill("aeolus-platform-analysis", "scripts", "url_query.py")
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

    The sheet has two frozen header rows (row 1 = merged title banner, row 2 =
    column labels such as "UPC"/"ISRC"/"Spotify"). Data begins at row 3. We also
    defensively skip any row whose UPC cell equals the header label.

    Returns ``(header, rows_to_process, skipped_fully_delivered_count)`` where
    each row item is ``(sheet_row_number, row_values)``.
    """
    values = extract_sheet_values(sheet_values_api("GET", sheet_url, sheet_id, read_range))
    if not values:
        return [], [], 0

    header = list(values[0] or [])
    rows_to_process: List[Tuple[int, List[object]]] = []
    skipped_fully_delivered = 0

    for row_num, raw_row in enumerate(values[HEADER_ROW_COUNT:], start=HEADER_ROW_COUNT + 1):
        row = list(raw_row or [])
        if not any(str(cell or "").strip() for cell in row):
            continue
        # Defensive: never treat the column-label row (or a duplicate header) as
        # data.
        if _cell(row, UPC_COLUMN).strip().upper() == UPC_HEADER_LABEL.upper():
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
    """Parse url_query.py stdout into a dict.

    url_query.py emits a single top-level JSON object on stdout (diagnostic logs
    go to stderr). The previous ``rfind("\\n{")`` heuristic grabbed the *last*
    nested object (e.g. the final row inside ``"rows": [...]``) instead of the
    outer object, so ``json.loads`` failed and every result came back empty —
    causing every DSP to be marked "Not Sent". Parse directly first, then fall
    back to brace-scanning, then to the on-disk ``dataFile`` (which holds full
    rows even when stdout truncates them).
    """
    text = (stdout or "").strip()
    if not text:
        return {}

    # 1) Fast path: stdout is the complete JSON object.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return _augment_with_datafile(parsed)
    except json.JSONDecodeError:
        pass

    # 2) Brace-scan for the first balanced top-level object (tolerates any
    #    preamble/trailing text).
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(text[start:i + 1])
                    if isinstance(parsed, dict):
                        return _augment_with_datafile(parsed)
                except json.JSONDecodeError:
                    start = -1

    # 3) Legacy fallback (kept for safety).
    for marker in ("\n{", "{"):
        idx = text.rfind(marker)
        if idx >= 0:
            candidate = text[idx + (1 if marker.startswith("\n") else 0):]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return _augment_with_datafile(parsed)
            except json.JSONDecodeError:
                continue
    return {}


def _augment_with_datafile(parsed: Dict[str, object]) -> Dict[str, object]:
    """If stdout rows were truncated, load the full row set from the dataFile."""
    if not isinstance(parsed, dict):
        return parsed
    rows = parsed.get("rows")
    total = parsed.get("total") or parsed.get("totalRows")
    truncated = parsed.get("truncated") or parsed.get("data_truncated")
    if (truncated or (isinstance(total, int) and isinstance(rows, list) and len(rows) < total)):
        data_file = parsed.get("dataFile")
        if isinstance(data_file, str) and data_file:
            try:
                with open(data_file, "r", encoding="utf-8") as fh:
                    full = json.load(fh)
                if isinstance(full, dict) and isinstance(full.get("rows"), list):
                    parsed["rows"] = full["rows"]
                elif isinstance(full, list):
                    parsed["rows"] = full
            except (OSError, json.JSONDecodeError):
                pass
    return parsed


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
    return _map_audiosalad_rows_to_statuses(payload.get("rows") or [])


def _map_audiosalad_rows_to_statuses(rows: Iterable[object]) -> Dict[str, str]:
    """Map AudioSalad delivery rows to per-DSP ✅/Not Sent statuses."""
    delivered_targets: set = set()
    for row in rows:
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


def query_audiosalad_statuses_batch(
    upcs: Sequence[str],
    chunk_size: int = 25,
    top_n: int = 500,
) -> Dict[str, Dict[str, str]]:
    """Query AudioSalad delivery status for many UPCs in batched Aeolus calls.

    The DataQuery report accepts a multi-value ``in`` filter (``upc=A,B,C``), so a
    single call returns delivery rows for a chunk of UPCs. Each UPC maps to its
    own status dict; UPCs absent from the response get all "Not Sent".
    """
    # Preserve order, deduplicate, and drop empties.
    seen: set = set()
    ordered: List[str] = []
    for raw in upcs:
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)

    result: Dict[str, Dict[str, str]] = {u: {name: NOT_SENT_MARK for name in DSP_STATUS_COLUMNS} for u in ordered}

    for start in range(0, len(ordered), chunk_size):
        chunk = ordered[start:start + chunk_size]
        filter_value = ",".join(chunk)
        try:
            payload = _query_aeolus_url(
                AUDIOSALAD_STATUS_URL,
                [f"{AUDIOSALAD_UPC_FILTER_FIELD}={filter_value}"],
                top_n=top_n,
            )
        except Exception as exc:  # noqa: BLE001 — one failed chunk shouldn't kill the batch
            print(
                f"⚠ AudioSalad batch query failed for {len(chunk)} UPCs "
                f"({type(exc).__name__}: {exc}); marking chunk Not Sent.",
                flush=True,
            )
            continue

        # Group returned delivery rows by UPC, then map each group.
        grouped: Dict[str, List[object]] = {}
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            row_upc = str(row.get(AUDIOSALAD_UPC_FILTER_FIELD) or "").strip()
            if row_upc in result:
                grouped.setdefault(row_upc, []).append(row)

        for row_upc, group_rows in grouped.items():
            result[row_upc] = _map_audiosalad_rows_to_statuses(group_rows)

        total_rows = payload.get("total")
        if isinstance(total_rows, int) and total_rows >= top_n:
            print(
                f"⚠ AudioSalad batch hit top_n={top_n} (total={total_rows}); "
                f"some delivery rows may be missing. Consider reducing chunk_size.",
                flush=True,
            )

    return result


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
    total = len(rows)
    upcs_filled = 0
    queried = 0
    written = 0
    row_errors = 0

    if not rows:
        summary = {
            "rows_to_process": 0,
            "skipped_fully_delivered": skipped,
            "queried_rows": 0,
            "upcs_filled": 0,
            "written_cells": 0,
            "row_errors": 0,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return summary

    # ------------------------------------------------------------------
    # Pass 1: resolve missing UPCs from ISRC (column K → column J).
    # ------------------------------------------------------------------
    # The UPC that will be queried for each row: existing J value, or the
    # value resolved via the ISRC lookup dashboard.
    row_upcs: List[str] = []
    upc_fill_cells: List[Tuple[int, str]] = []  # (sheet_row_number, resolved_upc)
    for idx, (row_num, row) in enumerate(rows, start=1):
        upc = _cell(row, UPC_COLUMN)
        if not upc:
            isrc = _cell(row, ISRC_COLUMN)
            if isrc:
                try:
                    upc = lookup_upc_by_isrc(isrc)
                except Exception as exc:  # noqa: BLE001
                    row_errors += 1
                    print(
                        f"[{idx}/{total}] row {row_num}: ISRC→UPC lookup failed "
                        f"({type(exc).__name__}: {exc}); continuing without UPC.",
                        flush=True,
                    )
                    upc = ""
                if upc:
                    j_idx = _col_index(UPC_COLUMN)
                    if len(row) <= j_idx:
                        row.extend([""] * (j_idx + 1 - len(row)))
                    row[j_idx] = upc
                    upc_fill_cells.append((row_num, upc))
                    upcs_filled += 1
        row_upcs.append(upc)

    # ------------------------------------------------------------------
    # Pass 2: batch-query AudioSalad delivery status for all UPCs.
    # ------------------------------------------------------------------
    unique_upcs = [u for u in dict.fromkeys(row_upcs) if u]
    print(
        f"Querying AudioSalad delivery status for {len(unique_upcs)} unique UPCs "
        f"({total} rows, {upcs_filled} UPCs filled from ISRC)...",
        flush=True,
    )
    upc_statuses = query_audiosalad_statuses_batch(unique_upcs)
    queried = sum(1 for u in row_upcs if u)

    # ------------------------------------------------------------------
    # Pass 3: build all cell writes and batch them.
    # ------------------------------------------------------------------
    # Each row writes its six DSP statuses as one contiguous range P{row}:U{row}.
    # UPC fills (column J) are separate single-cell ranges.  TikTok column O is
    # never touched.
    value_ranges: List[Dict[str, object]] = []
    for (row_num, _row), upc in zip(rows, row_upcs):
        statuses = upc_statuses.get(upc) if upc else None
        if statuses is None:
            statuses = {name: NOT_SENT_MARK for name in DSP_STATUS_COLUMNS}
        dsp_order = tuple(DSP_STATUS_COLUMNS.keys())  # spotify, facebook, ...
        dsp_values = [str(statuses.get(name, NOT_SENT_MARK)) for name in dsp_order]
        value_ranges.append({
            "range": f"{DSP_STATUS_SHEET_ID}!{DSP_STATUS_COLUMN_LETTERS[0]}{row_num}:{DSP_STATUS_COLUMN_LETTERS[-1]}{row_num}",
            "values": [dsp_values],
        })
    for row_num, upc_value in upc_fill_cells:
        value_ranges.append({
            "range": f"{DSP_STATUS_SHEET_ID}!{UPC_COLUMN}{row_num}:{UPC_COLUMN}{row_num}",
            "values": [[upc_value]],
        })

    if dry_run:
        written = len(value_ranges)
        print(f"(dry-run) would write {written} range(s).", flush=True)
    else:
        # Write each range individually via the working single-range PUT
        # endpoint. Each row's six DSP statuses are one contiguous range
        # P{row}:U{row}, so this is 1 API call per row (not 6). UPC fills are
        # single-cell ranges. TikTok column O is never touched.
        for idx, vr in enumerate(value_ranges, start=1):
            a1_range = str(vr["range"]).split("!", 1)[1] if "!" in str(vr["range"]) else str(vr["range"])
            try:
                sheet_values_api("PUT", DSP_STATUS_SHEET_URL, DSP_STATUS_SHEET_ID, a1_range, values=vr["values"])
                written += 1
            except Exception as exc:  # noqa: BLE001
                row_errors += 1
                print(
                    f"⚠ write failed for range {a1_range} "
                    f"({type(exc).__name__}: {str(exc)[:200]}); continuing.",
                    flush=True,
                )
            if idx % 50 == 0:
                print(f"  wrote {idx}/{len(value_ranges)} ranges...", flush=True)

    summary = {
        "rows_to_process": total,
        "skipped_fully_delivered": skipped,
        "queried_rows": queried,
        "upcs_filled": upcs_filled,
        "written_ranges": written,
        "row_errors": row_errors,
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
