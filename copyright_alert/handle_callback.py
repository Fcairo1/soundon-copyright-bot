#!/usr/bin/env python3
"""Handle Lark card action callbacks for copyright alert status updates.

Input: JSON payload via first CLI argument or stdin. Expected value payload:
{
  "action": "copyright_alert_status_update",
  "status": "🔍 Investigating",
  "message_id": "om_xxx",
  "upc": "...",
  "isrc": "..."
}
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from copyright_alert.lark_auth import request_json_with_auth_retry
from copyright_alert.run_alert import BOT_APP_ID, BOT_SECRET, build_card, _get_bot_access_token, load_posted_card
from copyright_alert import run_alert as ra

SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/HMQLsGgymhdIQ3tSbNNlk3m1gKd"
SHEET_ID = "c02dad"
STATUS_COL = "M"
MESSAGE_ID_COL = "Lark Message ID"
STATUS_COL_NAME = "Status"
UPC_COL_NAME = "UPC"
ISRC_COL_NAME = "ISRC"
EMAIL_STATUS_COL_NAME = "Email Status"
EMAIL_STATUS_COL = "S"


def _tracker_config(region=None):
    """Return tracker URL/sheet for a region, defaulting to the legacy BR tracker."""
    if region:
        try:
            from copyright_alert.bot_runtime import REGION_CONFIGS
            cfg = REGION_CONFIGS.get(str(region).strip().upper())
            if cfg:
                return cfg.get("tracker_url") or SHEET_URL, cfg.get("sheet_id") or SHEET_ID
        except Exception as exc:
            print(f"_tracker_config({region!r}) failed: {exc!r}", flush=True)
    return SHEET_URL, SHEET_ID


def _col_letter(index):
    """Convert a zero-based column index to a spreadsheet column letter."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _norm(value):
    return str(value or "").strip()


def _spreadsheet_token(sheet_url):
    """Extract the spreadsheet token from a Lark /sheets/ or /spreadsheets/ URL."""
    parsed = urllib.parse.urlparse(sheet_url)
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("sheets", "spreadsheets"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    # Allow callers to pass a bare token in tests or manual repair scripts.
    return sheet_url.strip()


def _sheet_api(method, sheet_url, sheet_id, cell_range, values=None, timeout=60):
    """Read/write sheet values through Lark OpenAPI using the bot tenant token.

    Callback daemons must not rely on lark-cli's injected user/AIME JWT because
    that credential can expire while the long-running daemon stays alive. The bot
    tenant_access_token is obtained via the app_id/app_secret flow and refreshed
    per request by _get_bot_access_token().
    """
    token = _get_bot_access_token()
    if not token:
        raise RuntimeError("Could not get bot access token")
    spreadsheet_token = _spreadsheet_token(sheet_url)
    a1_range = f"{sheet_id}!{cell_range}"
    encoded_range = urllib.parse.quote(a1_range, safe="")
    url = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{encoded_range}"
    body = None
    if values is not None:
        body = json.dumps({"valueRange": {"range": a1_range, "values": values}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8") or "{}")
    if payload.get("code") != 0:
        raise RuntimeError(f"Sheet API {method} {a1_range} failed: {json.dumps(payload, ensure_ascii=False)[:1000]}")
    return payload


def _read_sheet_values_cli(sheet_url, sheet_id):
    cmd = [
        "lark-cli", "sheets", "+csv-get", "--url", sheet_url,
        "--sheet-id", sheet_id, "--range", "A1:Z500",
    ]
    last_error = ""
    for attempt in range(1, 4):
        try:
            from copyright_alert.lark_auth import _refresh_aime_credentials
            refreshed = _refresh_aime_credentials()
            if refreshed:
                print(f"Sheet read: refreshed {refreshed} AIME credential key(s) before lark-cli attempt {attempt}", flush=True)
        except Exception as refresh_exc:
            print(f"Sheet read user-credential refresh skipped before lark-cli attempt {attempt}: {refresh_exc!r}", flush=True)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        combined = (res.stdout or "") + (res.stderr or "")
        parsed, rows, _ = ra.parse_lark_annotated_csv(res.stdout)
        if res.returncode == 0 and parsed:
            return rows
        last_error = combined[:2000]
        print(f"Sheet read via lark-cli attempt {attempt}/3 failed: {last_error[:800]}", flush=True)
        if attempt < 3:
            import time
            time.sleep(2 * attempt)
    raise RuntimeError(last_error[:1200])


def _write_sheet_cell_cli(sheet_url, sheet_id, cell, value):
    cmd = [
        "lark-cli", "sheets", "+cells-set", "--url", sheet_url,
        "--sheet-id", sheet_id, "--range", cell,
        "--cells", json.dumps([ [{"value": str(value if value is not None else "")}] ], ensure_ascii=False),
    ]
    try:
        from copyright_alert.lark_auth import _refresh_aime_credentials
        refreshed = _refresh_aime_credentials()
        if refreshed:
            print(f"Sheet write: refreshed {refreshed} AIME credential key(s) before lark-cli update {cell}", flush=True)
    except Exception as refresh_exc:
        print(f"Sheet write user-credential refresh skipped before lark-cli update {cell}: {refresh_exc!r}", flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError((res.stdout + res.stderr)[:1200])
    return True


def load_payload():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    payload = json.loads(raw)
    action = payload.get("action")
    if isinstance(action, dict):
        return payload.get("value") or action.get("value") or payload
    return payload.get("value") or payload


def load_last_card():
    return json.loads(Path("copyright_alert/last_card.json").read_text())


def load_card_for_message(message_id):
    return load_posted_card(message_id) or load_last_card()


def _button_type_for_status(status):
    return "danger" if status and (status.startswith("🔴") or "Takedown" in status) else "primary"


def _status_audit_line(status, operator_name=None, operator_id=None, timestamp=None):
    if not status:
        return "**Status: No action yet**"
    who = operator_name or operator_id or "unknown user"
    when = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"**Status: {status} — set by {who} at {when}**"


def update_card_state(card, status, message_id, operator_name=None, operator_id=None, timestamp=None):
    card.setdefault("config", {})["wide_screen_mode"] = True
    card["config"]["update_multi"] = True
    card.setdefault("header", {
        "template": "red",
        "title": {"tag": "plain_text", "content": "🚨 Copyright Infringement Alert"},
    })
    effective_status = status or ""
    audit_line = _status_audit_line(effective_status, operator_name, operator_id, timestamp)

    # Header summary/audit line
    first = card["elements"][0]["text"]["content"].split("\n")
    first = [line for line in first if not line.startswith("**Status:")]
    first.append(audit_line)
    card["elements"][0]["text"]["content"] = "\n".join(first)

    selected_type = _button_type_for_status(effective_status)
    for el in card.get("elements", []):
        if el.get("tag") != "action":
            continue
        for btn in el.get("actions", []):
            value = btn.setdefault("value", {})
            value["message_id"] = message_id
            value["current_status"] = effective_status
            btn["type"] = selected_type if effective_status and value.get("status") == effective_status else "default"
    return card


def patch_message(message_id, card):
    content_json = json.dumps(card, ensure_ascii=False)
    Path("copyright_alert/last_patch_card.json").write_text(content_json, encoding="utf-8")
    print("PATCH card JSON:", content_json, flush=True)

    def make_request():
        token = _get_bot_access_token()
        if not token:
            raise RuntimeError("Could not get bot access token")
        url = f"https://open.larksuite.com/open-apis/im/v1/messages/{message_id}"
        body = json.dumps({"msg_type": "interactive", "content": content_json}, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(
            url,
            data=body,
            method="PATCH",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )

    try:
        result = request_json_with_auth_retry(make_request, timeout=15, context=f"handle_callback.patch_message:{message_id}")
        print("PATCH body:", json.dumps(result, ensure_ascii=False), flush=True)
        return result.get("code") == 0
    except Exception as exc:
        print("PATCH failed:", repr(exc), flush=True)
        return False


def read_sheet_values(region=None):
    sheet_url, sheet_id = _tracker_config(region)
    # Tracker reads must use the current user's OAuth context. The BR tracker is
    # not readable by the bot tenant token (403), and returning an empty list on
    # that path makes commands like `/card <UPC>` incorrectly report "not found".
    # lark-cli defaults to user identity in AIME and is the same auth path used by
    # the daily scan/tracker workflows.
    try:
        try:
            from copyright_alert.lark_auth import _refresh_aime_credentials
            _refresh_aime_credentials()
        except Exception as refresh_exc:
            print(f"Sheet read user-credential refresh skipped: {refresh_exc!r}", flush=True)
        return _read_sheet_values_cli(sheet_url, sheet_id)
    except Exception as exc:
        print(f"Sheet read via lark-cli user OAuth failed; falling back to bot token: {exc!r}", flush=True)
    try:
        data = _sheet_api("GET", sheet_url, sheet_id, "A1:Z500")
        value_range = (data.get("data") or {}).get("valueRange") or {}
        raw_values = value_range.get("values") or []
        rows = []
        for row in raw_values:
            row = list(row or [])
            if len(row) < 26:
                row.extend([""] * (26 - len(row)))
            rows.append(row[:26])
        return rows
    except Exception as exc:
        print(f"Sheet read failed via bot token fallback: {exc!r}", flush=True)
        return []


def update_sheet_status(message_id, status, upc=None, isrc=None, region=None, tracker_row=None):
    sheet_url, sheet_id = _tracker_config(region)
    values = read_sheet_values(region=region)
    if not values:
        print("Sheet row not found: sheet is empty or unreadable")
        return False

    headers = [_norm(v) for v in values[0]]
    header_index = {name: idx for idx, name in enumerate(headers) if name}
    status_idx = header_index.get(STATUS_COL_NAME)
    message_idx = header_index.get(MESSAGE_ID_COL)
    upc_idx = header_index.get(UPC_COL_NAME)
    isrc_idx = header_index.get(ISRC_COL_NAME)

    if status_idx is None:
        status_idx = ord(STATUS_COL) - ord("A")
    if message_idx is None:
        print("Sheet is missing Lark Message ID column; headers:", headers)

    row_num = None
    match_reason = None
    if tracker_row not in (None, ""):
        try:
            candidate_row = int(tracker_row)
            if 2 <= candidate_row <= len(values):
                row_num = candidate_row
                match_reason = "tracker_row"
        except (TypeError, ValueError):
            print(f"Invalid tracker_row for status update: {tracker_row!r}")
    for idx, row in enumerate(values[1:], start=2):
        if row_num:
            break
        msg_cell = _norm(row[message_idx]) if message_idx is not None and len(row) > message_idx else ""
        upc_cell = _norm(row[upc_idx]) if upc_idx is not None and len(row) > upc_idx else ""
        isrc_cell = _norm(row[isrc_idx]) if isrc_idx is not None and len(row) > isrc_idx else ""

        if msg_cell and msg_cell == _norm(message_id):
            row_num = idx
            match_reason = "message_id"
            break
        if upc and upc_cell and upc_cell == _norm(upc):
            row_num = idx
            match_reason = "upc"
            break
        if isrc and isrc_cell and isrc_cell == _norm(isrc):
            row_num = idx
            match_reason = "isrc"
            break

    if not row_num:
        print("Sheet row not found for", json.dumps({"message_id": message_id, "upc": upc, "isrc": isrc}, ensure_ascii=False))
        return False

    updates = [(_col_letter(status_idx), str(status if status is not None else ""))]
    if message_idx is not None:
        current_msg = _norm(values[row_num - 1][message_idx]) if len(values[row_num - 1]) > message_idx else ""
        message_id_str = str(message_id if message_id is not None else "")
        if current_msg != _norm(message_id_str):
            updates.append((_col_letter(message_idx), message_id_str))

    ok = True
    for col, value in updates:
        value_str = str(value if value is not None else "")
        cell = f"{col}{row_num}"
        try:
            _sheet_api("PUT", sheet_url, sheet_id, cell, values=[[value_str]])
            print(f"Sheet update {cell} via bot token: ok", flush=True)
        except Exception as exc:
            print(f"Sheet update {cell} via bot token failed; falling back to lark-cli: {exc!r}", flush=True)
            try:
                _write_sheet_cell_cli(sheet_url, sheet_id, cell, value_str)
                print(f"Sheet update {cell} via lark-cli fallback: ok", flush=True)
            except Exception as fallback_exc:
                print(f"Sheet update {cell} via lark-cli fallback failed: {fallback_exc!r}", flush=True)
                ok = False
    print("Sheet row matched by:", match_reason, "row:", row_num)
    return ok


def _find_col_letter(header_name, fallback_letter, region=None):
    """Resolve a column letter by header name, falling back to a fixed letter."""
    try:
        values = read_sheet_values(region=region)
        if values:
            headers = [_norm(v) for v in values[0]]
            for idx, name in enumerate(headers):
                if name == header_name:
                    return _col_letter(idx)
    except Exception as exc:
        print(f"_find_col_letter({header_name}) failed: {exc!r}")
    return fallback_letter


def update_sheet_email_status(tracker_row, value, region=None):
    """Write `value` to the 'Email Status' column for the given tracker row number."""
    if not tracker_row:
        print("update_sheet_email_status: missing tracker_row")
        return False
    try:
        row_num = int(tracker_row)
    except (TypeError, ValueError):
        print(f"update_sheet_email_status: invalid tracker_row {tracker_row!r}")
        return False

    sheet_url, sheet_id = _tracker_config(region)
    col = _find_col_letter(EMAIL_STATUS_COL_NAME, EMAIL_STATUS_COL, region=region)
    val_str = str(value if value is not None else "")
    cell = f"{col}{row_num}"
    try:
        _sheet_api("PUT", sheet_url, sheet_id, cell, values=[[val_str]])
        print(f"Email status update {cell} via bot token: ok", flush=True)
        return True
    except Exception as exc:
        print(f"Email status update {cell} via bot token failed; falling back to lark-cli: {exc!r}", flush=True)
    try:
        _write_sheet_cell_cli(sheet_url, sheet_id, cell, val_str)
        print(f"Email status update {cell} via lark-cli fallback: ok", flush=True)
        return True
    except Exception as exc:
        print(f"Email status update {cell} via lark-cli fallback failed: {exc!r}", flush=True)
        return False


def main():
    payload = load_payload()
    status = payload.get("status")
    message_id = payload.get("message_id")
    if not status or not message_id:
        raise SystemExit("Missing status or message_id in payload")

    card = update_card_state(load_card_for_message(message_id), status, message_id)
    Path("copyright_alert/last_card_callback.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))

    patched = patch_message(message_id, card)
    sheet_ok = update_sheet_status(message_id, status, upc=payload.get("upc"), isrc=payload.get("isrc"), region=payload.get("region"), tracker_row=payload.get("tracker_row"))
    print(json.dumps({"patched": patched, "sheet_updated": sheet_ok, "status": status, "message_id": message_id, "region": payload.get("region")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
