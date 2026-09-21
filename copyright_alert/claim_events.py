#!/usr/bin/env python3
"""Claim Event Log — append-only timeline used to measure manager reply /
resolution / handling time and ops handling time on the Infringement Claims
dashboard.

Sheet columns (all timestamps UTC): claim_key | timestamp_utc | event_type |
status | source | actor | region | prev_status | ref_code | upc

claim_key is the claim's group-card "Lark Message ID" (present on every
tracker row; ref_code is missing on ~30% of rows and not unique).

event_type:
  card_posted        when the claim card was posted (Lark message create_time)
  am_action          a manager status change (card button here; the dashboard
                     writes the same event type with source="dashboard")
  admin_action_seen  first time "Admin Action Taken" was seen filled for a claim
                     that a manager had already moved to Confirm Takedown /
                     Resolved (ops handling clock; accurate to the daily run)

Every write is best-effort: a failure here must never break a card click or
the daily workflow. Failed appends are queued in runtime/ and retried.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVENT_LOG_SHEET_URL = os.getenv(
    "CLAIM_EVENT_LOG_SHEET_URL",
    "https://bytedance.sg.larkoffice.com/sheets/QdR2sU0F1h0obdtIlm8laxKNgYf",
)
EVENT_LOG_SHEET_ID = os.getenv("CLAIM_EVENT_LOG_SHEET_ID", "8d1783")

COLUMNS = [
    "claim_key", "timestamp_utc", "event_type", "status", "source",
    "actor", "region", "prev_status", "ref_code", "upc",
]
PENDING_FILE = ROOT / "runtime" / "claim_events_pending.jsonl"

MESSAGE_ID_HEADER = "Lark Message ID"
STATUS_HEADER = "Status"
DATE_RECEIVED_HEADER = "Date Received"
ADMIN_ACTION_HEADER = "Admin Action Taken"
UPC_HEADER = "UPC"
REF_CODE_HEADERS = ("ref_code", "Spotify Ref Code")

AM_TERMINAL_MARKERS = ("confirm takedown", "resolved")
MAX_CREATE_TIME_LOOKUPS_PER_RUN = 450


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ms_to_utc_str(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _norm(value) -> str:
    return str(value or "").strip()


def _status_text(value) -> str:
    """Lower-case status with any leading emoji/symbols removed."""
    text = _norm(value).lower()
    return "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip()


def is_am_terminal(status) -> bool:
    text = _status_text(status)
    return any(marker in text for marker in AM_TERMINAL_MARKERS)


def _build_row(claim_key, event_type, status, source, region, *, ts=None, actor="", prev_status="", ref_code="", upc="") -> List[str]:
    return [
        _norm(claim_key), ts or utc_now_str(), event_type, _norm(status), source,
        _norm(actor), _norm(region), _norm(prev_status), _norm(ref_code), _norm(upc),
    ]


# ── Sheet I/O (user-OAuth, same auth the tracker sheets use) ────────────────

def _append_rows(rows: List[List[str]]) -> None:
    from copyright_alert.lark_auth import _spreadsheet_token, get_user_access_token, request_json_with_auth_retry

    token = _spreadsheet_token(EVENT_LOG_SHEET_URL)
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{token}/values_append?insertDataOption=INSERT_ROWS"
    last_col = chr(64 + len(COLUMNS))
    body = json.dumps(
        {"valueRange": {"range": f"{EVENT_LOG_SHEET_ID}!A:{last_col}", "values": rows}}, ensure_ascii=False
    ).encode("utf-8")

    def make_request():
        return urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {get_user_access_token()}",
            },
        )

    payload = request_json_with_auth_retry(make_request, timeout=20, context="claim_events.append")
    if payload.get("code") not in (0, "0", None):
        raise RuntimeError(f"Event log append failed: {json.dumps(payload, ensure_ascii=False)[:500]}")


def _queue_pending(rows: List[List[str]]) -> None:
    try:
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PENDING_FILE.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"⚠ claim_events: could not queue pending rows: {exc!r}", flush=True)


def flush_pending() -> int:
    """Retry rows that failed to append earlier. Returns how many were sent."""
    if not PENDING_FILE.exists():
        return 0
    try:
        rows = [json.loads(line) for line in PENDING_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception as exc:
        print(f"⚠ claim_events: unreadable pending file: {exc!r}", flush=True)
        return 0
    if not rows:
        return 0
    try:
        _append_rows(rows)
    except Exception as exc:
        print(f"⚠ claim_events: pending flush failed, will retry later: {exc!r}", flush=True)
        return 0
    PENDING_FILE.unlink(missing_ok=True)
    return len(rows)


def append_event(claim_key, event_type, status, source, region, **kwargs) -> bool:
    """Append one event. Never raises; on failure the row is queued for retry."""
    if not _norm(claim_key):
        return False
    row = _build_row(claim_key, event_type, status, source, region, **kwargs)
    try:
        _append_rows([row])
        return True
    except Exception as exc:
        print(f"⚠ claim_events: append failed, queued for retry: {exc!r}", flush=True)
        _queue_pending([row])
        return False


def read_events() -> List[Dict[str, str]]:
    from copyright_alert.lark_auth import extract_sheet_values, sheet_values_api

    last_col = chr(64 + len(COLUMNS))
    values = extract_sheet_values(
        sheet_values_api("GET", EVENT_LOG_SHEET_URL, EVENT_LOG_SHEET_ID, f"A1:{last_col}30000")
    )
    events = []
    for raw in values[1:]:
        entry = {col: _norm(raw[i]) if i < len(raw) else "" for i, col in enumerate(COLUMNS)}
        if entry["claim_key"]:
            events.append(entry)
    return events


# ── Hook: manager status write (called from handle_callback.update_sheet_status)

def _row_field(row: Sequence[object], header_index: Dict[str, int], *names: str) -> str:
    for name in names:
        idx = header_index.get(name)
        if idx is not None and idx < len(row):
            return _norm(row[idx])
    return ""


def record_status_event(values, header_index, row_num, *, new_status, region, key_hint="") -> bool:
    """Log an am_action for the tracker row that just had its Status written.

    `values` is the sheet snapshot read BEFORE the write, so the row's Status
    cell still holds the previous value. No-op re-clicks are skipped.
    """
    row = values[row_num - 1]
    prev_status = _row_field(row, header_index, STATUS_HEADER)
    if _status_text(prev_status) == _status_text(new_status):
        return False
    claim_key = _row_field(row, header_index, MESSAGE_ID_HEADER) or _norm(key_hint)
    return append_event(
        claim_key, "am_action", new_status, "card", region,
        prev_status=prev_status,
        ref_code=_row_field(row, header_index, *REF_CODE_HEADERS),
        upc=_row_field(row, header_index, UPC_HEADER),
    )


# ── Daily sync: card_posted backfill + admin_action_seen ────────────────────

def get_message_create_time(message_id: str) -> Optional[str]:
    """Lark message create_time (ms epoch) as a UTC string, or None."""
    from copyright_alert import run_alert as ra

    token = ra._get_bot_access_token()
    if not token:
        return None
    req = urllib.request.Request(
        f"https://open.larksuite.com/open-apis/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
        items = (payload.get("data") or {}).get("items") or []
        if payload.get("code") == 0 and items and items[0].get("create_time"):
            return ms_to_utc_str(items[0]["create_time"])
    except Exception as exc:
        print(f"  claim_events: create_time lookup failed for {message_id}: {exc!r}", flush=True)
    return None


def plan_card_posted(values, events: Iterable[Dict[str, str]], region: str) -> List[Dict[str, str]]:
    """Tracker rows with a group-card message id but no card_posted event yet."""
    if not values:
        return []
    header_index = {_norm(v): i for i, v in enumerate(values[0]) if _norm(v)}
    have = {e["claim_key"] for e in events if e["event_type"] == "card_posted"}
    todo, seen = [], set()
    for row in values[1:]:
        key = _row_field(row, header_index, MESSAGE_ID_HEADER)
        if not key or key in have or key in seen:
            continue
        seen.add(key)
        todo.append({
            "claim_key": key,
            "ref_code": _row_field(row, header_index, *REF_CODE_HEADERS),
            "upc": _row_field(row, header_index, UPC_HEADER),
            "region": region,
        })
    return todo


def plan_admin_action_seen(values, events: Iterable[Dict[str, str]], region: str) -> List[Dict[str, str]]:
    """Claims a manager already moved to Confirm Takedown / Resolved whose
    Admin Action Taken is now filled (and not a plain "No"), first time only."""
    if not values:
        return []
    events = list(events)
    header_index = {_norm(v): i for i, v in enumerate(values[0]) if _norm(v)}
    waiting = {e["claim_key"] for e in events if e["event_type"] == "am_action" and is_am_terminal(e["status"])}
    done = {e["claim_key"] for e in events if e["event_type"] == "admin_action_seen"}
    out = []
    for row in values[1:]:
        key = _row_field(row, header_index, MESSAGE_ID_HEADER)
        admin = _row_field(row, header_index, ADMIN_ACTION_HEADER)
        if not key or key not in waiting or key in done:
            continue
        if admin.lower() in ("", "no"):
            continue
        out.append({
            "claim_key": key, "admin_action": admin, "region": region,
            "ref_code": _row_field(row, header_index, *REF_CODE_HEADERS),
            "upc": _row_field(row, header_index, UPC_HEADER),
        })
    return out


def run_daily_sync(region: str, *, dry_run: bool = False,
                   create_time_fn: Callable[[str], Optional[str]] = None) -> Dict[str, object]:
    from copyright_alert import handle_callback

    create_time_fn = create_time_fn or get_message_create_time
    summary: Dict[str, object] = {"region": region, "dry_run": dry_run}
    if not dry_run:
        summary["pending_flushed"] = flush_pending()
    values = handle_callback.read_sheet_values(region)
    events = read_events()

    card_todo = plan_card_posted(values, events, region)
    rows_to_append: List[List[str]] = []
    looked_up = skipped = 0
    for item in card_todo[:MAX_CREATE_TIME_LOOKUPS_PER_RUN]:
        ts = create_time_fn(item["claim_key"])
        looked_up += 1
        if not ts:
            skipped += 1
            continue
        rows_to_append.append(_build_row(
            item["claim_key"], "card_posted", "", "backfill", region,
            ts=ts, ref_code=item["ref_code"], upc=item["upc"],
        ))
        time.sleep(0.1)

    seen_todo = plan_admin_action_seen(values, events, region)
    for item in seen_todo:
        rows_to_append.append(_build_row(
            item["claim_key"], "admin_action_seen", item["admin_action"], "bot_detect", region,
            ref_code=item["ref_code"], upc=item["upc"],
        ))

    summary.update({
        "card_posted_missing": len(card_todo), "create_time_lookups": looked_up,
        "create_time_failed": skipped, "admin_action_seen_new": len(seen_todo),
        "rows_to_append": len(rows_to_append),
    })
    if rows_to_append and not dry_run:
        _append_rows(rows_to_append)
    return summary


def run_daily_sync_safe(region: str) -> Dict[str, object]:
    """Entry point for daily_workflow.py — never raises."""
    try:
        result = run_daily_sync(region)
        print(f"  ✓ Claim event log sync: {json.dumps(result, ensure_ascii=False)}", flush=True)
        return result
    except Exception as exc:
        print(f"  ✗ Claim event log sync failed (non-fatal): {exc!r}", flush=True)
        return {"error": repr(exc)}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sync the Claim Event Log (card_posted backfill + admin_action_seen)")
    parser.add_argument("--region", required=True, help="BR, US or SPLA")
    parser.add_argument("--dry-run", action="store_true", help="Look up and plan, but write nothing")
    args = parser.parse_args()
    print(json.dumps(run_daily_sync(args.region.upper(), dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
