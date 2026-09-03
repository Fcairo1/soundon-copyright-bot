#!/usr/bin/env python3
"""Weekday BR sheet watcher for unclaimed ops-action rows.

Scans the BR ops sheet and DMs Filipe when rows still need BR ops action
(column C = BR and column O empty).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import bot_runtime  # noqa: E402
from copyright_alert import daily_workflow as dw  # noqa: E402
from copyright_alert.lark_auth import extract_sheet_values, sheet_values_api  # noqa: E402

HEADER_ROWS = 2
MAX_ROWS_IN_DM = 25


def _ops_sheet_config() -> Dict[str, str]:
    br_cfg = bot_runtime.REGION_CONFIGS.get("BR") or {}
    sheet_url = (os.getenv("BR_OPS_WATCHER_SHEET_URL") or br_cfg.get("ops_watcher_sheet_url") or "").strip()
    sheet_id = (os.getenv("BR_OPS_WATCHER_SHEET_ID") or br_cfg.get("ops_watcher_sheet_id") or "").strip()
    cell_range = (os.getenv("BR_OPS_WATCHER_RANGE") or br_cfg.get("ops_watcher_sheet_range") or "A:O").strip()
    if not sheet_url or not sheet_id:
        raise RuntimeError(
            "BR Sheet Ops Action Watcher sheet URL/ID is not configured. "
            "Set BR_OPS_WATCHER_SHEET_URL and BR_OPS_WATCHER_SHEET_ID or add them to REGION_CONFIGS['BR']."
        )
    return {"sheet_url": sheet_url, "sheet_id": sheet_id, "range": cell_range}


def _parse_lark_json(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


def read_sheet_values(sheet_url: str, sheet_id: str, cell_range: str) -> List[list]:
    try:
        payload = sheet_values_api("GET", sheet_url, sheet_id, cell_range)
        return extract_sheet_values(payload)
    except Exception as exc:
        print(f"⚠ Sheet read via OAuth failed; trying legacy lark-cli fallback: {exc!r}", flush=True)
    cmd = [
        "lark-cli",
        "sheets",
        "+read",
        "--url",
        sheet_url,
        "--sheet-id",
        sheet_id,
        "--range",
        cell_range,
        "--format",
        "json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"BR ops sheet read failed: {(res.stdout + res.stderr)[:400]}")
    parsed = _parse_lark_json(res.stdout)
    if not parsed:
        raise RuntimeError("BR ops sheet read did not return parseable JSON")
    return extract_sheet_values(parsed)


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("link") or json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                parts.append(str(item))
        return " | ".join(part.strip() for part in parts if str(part).strip())
    if isinstance(value, dict):
        return (
            value.get("text")
            or value.get("name")
            or value.get("link")
            or json.dumps(value, ensure_ascii=False, sort_keys=True)
        ).strip()
    return str(value).strip()


def _cell(row: list, idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return _normalize_cell(row[idx])


def collect_br_rows_needing_ops(values: List[list]) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    for row_number, row in enumerate(values[HEADER_ROWS:], start=HEADER_ROWS + 1):
        if not any(_cell(row, i) for i in range(min(len(row), 15))):
            continue
        region = _cell(row, 2)
        ops_action_done = _cell(row, 14)
        if region != "BR" or ops_action_done:
            continue
        matches.append(
            {
                "row_number": str(row_number),
                "user_name": _cell(row, 1),
                "region": region,
                "track_isrc": _cell(row, 4),
                "video_link": _cell(row, 6),
                "revenue_loss": _cell(row, 8),
                "reason": _cell(row, 9),
                "contract_type": _cell(row, 10),
                "product_ops_action": _cell(row, 13),
            }
        )
    return matches


def build_dm_lines(rows: List[Dict[str, str]], sheet_url: str) -> List[object]:
    lines: List[object] = [
        f"{len(rows)} BR row(s) still need ops action in the AMS Content ID Allowlist Approval sheet.",
        "__HR__",
    ]
    for row in rows[:MAX_ROWS_IN_DM]:
        summary = (
            f"• Row {row['row_number']} — {row['track_isrc'] or 'no ISRC'} — "
            f"{row['reason'] or 'no reason'}"
        )
        details = (
            f"  User: {row['user_name'] or 'n/a'} | Contract: {row['contract_type'] or 'n/a'} | "
            f"Revenue loss: {row['revenue_loss'] or 'n/a'} | Product Ops action: {row['product_ops_action'] or 'n/a'}"
        )
        lines.append(summary)
        lines.append(details)
        if row["video_link"]:
            lines.append(("link", f"Open video link for row {row['row_number']}", row["video_link"]))
    remaining = len(rows) - min(len(rows), MAX_ROWS_IN_DM)
    if remaining > 0:
        lines.append(f"Plus {remaining} more matching row(s) in the sheet.")
    lines.extend([
        "__HR__",
        ("link", "Open the BR ops sheet", sheet_url),
    ])
    return lines


def run_br_sheet_ops_watcher(*, dry_run: bool = False) -> Dict[str, object]:
    config = _ops_sheet_config()
    bot_runtime.configure_region("BR")
    dw.configure_region("BR")

    values = read_sheet_values(config["sheet_url"], config["sheet_id"], config["range"])
    matches = collect_br_rows_needing_ops(values)
    print(f"BR sheet ops watcher scanned {max(len(values) - HEADER_ROWS, 0)} data rows; matches={len(matches)}", flush=True)

    sent = False
    if matches:
        lines = build_dm_lines(matches, config["sheet_url"])
        if dry_run:
            print(json.dumps(lines, ensure_ascii=False, indent=2), flush=True)
        else:
            sent = dw.send_dm_post("🟡 BR sheet ops action reminder", lines)
            print(f"DM {'sent' if sent else 'failed'} to {dw.RECIPIENT_CHAT_ID or dw.RECIPIENT_OPEN_ID or dw.RECIPIENT_EMAIL}", flush=True)
    else:
        print("No BR rows currently need ops action.", flush=True)

    return {
        "sheet_url": config["sheet_url"],
        "sheet_id": config["sheet_id"],
        "scanned_rows": max(len(values) - HEADER_ROWS, 0),
        "matches": len(matches),
        "sent": sent,
        "dry_run": dry_run,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan the BR ops sheet and DM Filipe about untouched BR rows.")
    parser.add_argument("--dry-run", action="store_true", help="Print the DM payload without sending it")
    args = parser.parse_args()
    run_br_sheet_ops_watcher(dry_run=args.dry_run)
