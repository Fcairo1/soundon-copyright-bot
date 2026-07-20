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
import csv
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from copyright_alert.lark_auth import extract_sheet_values, request_json_with_auth_retry, sheet_values_api
from copyright_alert.run_alert import BOT_APP_ID, BOT_SECRET, build_card, _get_bot_access_token, load_posted_card
from copyright_alert import run_alert as ra

SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/HMQLsGgymhdIQ3tSbNNlk3m1gKd"
SHEET_ID = "c02dad"
# Tracker schema: Status = column N, Email Status = column T. These are only
# used as a last-resort fallback when the header lookup fails (see B1).
STATUS_COL = "N"
MESSAGE_ID_COL = "Lark Message ID"
STATUS_COL_NAME = "Status"
UPC_COL_NAME = "UPC"
ISRC_COL_NAME = "ISRC"
EMAIL_STATUS_COL_NAME = "Email Status"
EMAIL_STATUS_COL = "T"
# J4: Additional headers used by the row-matching priority hierarchy in
# _find_tracker_row. The group-card message id is mirrored into both the
# "Lark Message ID" (column P) and "Card Message ID" (column S) columns, and the
# Spotify claim reference code lives in the "ref_code" column (U). "Date Received"
# (column O) is used to disambiguate multiple rows that share a UPC.
CARD_MSG_ID_COL_NAME = "Card Message ID"
REF_CODE_COL_NAME = "ref_code"
DATE_RECEIVED_COL_NAME = "Date Received"
# F1: The bot ONLY writes to column N ("Status") via update_sheet_status().
# Column R ("Admin Action Taken") is filled MANUALLY by ops and must NEVER be
# written by the bot. The old dead admin-action machinery
# (update_sheet_admin_action / _admin_action_value_for_status /
# ADMIN_ACTION_COL_NAME="Bot Action" / ADMIN_ACTION_COL="N") was removed because
# it pointed at the wrong header name and the wrong column (N is Status, not
# Admin Action) and was a landmine waiting to corrupt the tracker.


# H1: Distinguish "the sheet could not be read at all" (empty/unreadable —
# usually a stale JWT in the long-lived daemon) from "the sheet was read fine
# but the header is missing" (a real layout change). Both subclass RuntimeError
# so existing `except RuntimeError` handlers keep working, but callers can now
# treat a transient read failure differently from a genuine layout problem.
class TrackerReadError(RuntimeError):
    """The tracker could not be read (empty/unreadable). Likely transient
    (stale credential / 403 fallback). Callers MAY fall back to a known-schema
    column letter because the header layout was never actually observed."""


class TrackerColumnMissingError(RuntimeError):
    """The tracker WAS read successfully but the requested header is absent —
    a genuine layout change. Callers must NEVER blind-write to a hardcoded
    fallback column here, or an unrelated column could be corrupted (B1)."""


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
    """Read/write sheet values through Lark OpenAPI using persisted user OAuth."""
    return sheet_values_api(method, sheet_url, sheet_id, cell_range, values=values, timeout=timeout)


def _parse_lark_json(raw):
    idx = (raw or "").find("{")
    if idx < 0:
        return None
    try:
        return json.loads(raw[idx:])
    except Exception:
        return None


def _parse_lark_annotated_csv(raw):
    parsed = _parse_lark_json(raw)
    if not parsed:
        return None, []
    data = parsed.get("data") or {}
    annotated_csv = data.get("annotated_csv") or ""
    cleaned_csv = re.sub(r"(?m)^\[row=\d+\]\s?", "", annotated_csv)
    rows = list(csv.reader(io.StringIO(cleaned_csv))) if cleaned_csv else []
    return parsed, rows


def _read_sheet_values_cli(sheet_url, sheet_id):
    cmd = [
        "lark-cli", "sheets", "+csv-get", "--url", sheet_url,
        "--sheet-id", sheet_id, "--range", "A1:Z2000",  # B11: uncapped from A1:Z500
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


# Maps the short keys expected by daily_workflow._reconstruct_card_from_row to
# the tracker header names, so we can rebuild a patchable card from the row.
_TRACKER_RECONSTRUCT_HEADER_KEYS = {
    "status": "Status",
    "date": "Date Received",
    "upc": "UPC",
    "isrc": "ISRC",
    "title": "Title",
    "artist": "Artist(s)",
    "claimant": "Claimant",
    "email_source": "Email Source",
    "dsp": "DSP",
    "uid": "UID",
    # J2: Map the short key used by daily_workflow._reconstruct_card_from_row
    # (cell("spotify_ref")) to the live tracker header "ref_code" (column U), so
    # the callback rebuild path can read the Spotify ref code and populate
    # ef["ref_id"]. Without this entry the header index was never resolved and
    # rebuilt cards always fell back to "N/A".
    "spotify_ref": "ref_code",
}


def _posted_claim_record_for_message(message_id):
    """Return checkpoint metadata for a card message id, including legacy files.

    This is a safe, read-only aid for legacy cards whose button value or tracker
    row is incomplete.  The live tracker remains the source for rebuilt card
    content; checkpoint metadata only supplies missing lookup keys such as UPC,
    ISRC, ref_code, Date Received, and tracker_row.
    """
    want = _norm(message_id)
    if not want:
        return {}
    try:
        posted = ra._load_posted_claims()
    except Exception as exc:
        print(f"posted_claim lookup failed for message_id={message_id}: {exc!r}", flush=True)
        return {}
    if not isinstance(posted, dict):
        return {}
    for record in posted.values():
        if isinstance(record, dict) and _norm(record.get("message_id")) == want:
            return record
    return {}


def reconstruct_card_from_tracker(message_id, region=None, upc=None, isrc=None, tracker_row=None, ref_id=None, date=None):
    """Rebuild a patchable card from the tracker row for a given claim.

    Used as a fallback when posted_cards.json has no persisted copy of the
    clicked card. This is strongly preferred over patching last_card.json —
    that file is the most-recently-posted card for some *other* claim and would
    silently replace the clicked card's content with the wrong data (B8).

    Returns the reconstructed card dict, or None if the row cannot be located
    or reconstruction fails.
    """
    claim_record = _posted_claim_record_for_message(message_id)
    if claim_record:
        region = region or claim_record.get("region")
        upc = upc or claim_record.get("upc")
        isrc = isrc or claim_record.get("isrc")
        tracker_row = tracker_row or claim_record.get("tracker_row")
        ref_id = ref_id or claim_record.get("ref_id")
        date = date or claim_record.get("date") or claim_record.get("date_received")
        print(
            "reconstruct_card_from_tracker: enriched lookup from posted_claims "
            + json.dumps(
                {
                    "message_id": message_id,
                    "region": region,
                    "upc": upc,
                    "isrc": isrc,
                    "tracker_row": tracker_row,
                    "ref_id": ref_id,
                    "date": date,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    try:
        values = read_sheet_values(region=region)
    except Exception as exc:
        print(f"reconstruct_card_from_tracker: could not read sheet: {exc!r}", flush=True)
        return None
    if not values:
        print(
            f"reconstruct_card_from_tracker: sheet read returned no rows "
            f"(region={region}); cannot rebuild message_id={message_id}. "
            f"This is usually a transient sheet-read/auth failure, not a missing row.",
            flush=True,
        )
        return None
    row_num, _match_reason, header_index = _find_tracker_row(
        values, message_id=message_id, upc=upc, isrc=isrc, ref_id=ref_id, date=date, tracker_row=tracker_row,
    )
    if not row_num:
        print(f"reconstruct_card_from_tracker: no tracker row for message_id={message_id}", flush=True)
        return None
    row = values[row_num - 1]
    idx = {short: header_index.get(header) for short, header in _TRACKER_RECONSTRUCT_HEADER_KEYS.items()}
    try:
        from copyright_alert.daily_workflow import _reconstruct_card_from_row
        # Thread the callback's region through so a rebuilt US/SPLA/BR card keeps
        # its own region context (PoC mentions, ops routing). Without this the
        # rebuild fell back to the daemon's global CURRENT_REGION (often "BR"),
        # producing a structurally-valid but wrong-region card.
        return _reconstruct_card_from_row(row, idx, message_id or "", region=region or "")
    except Exception as exc:
        print(f"reconstruct_card_from_tracker: rebuild failed: {exc!r}", flush=True)
        return None


def fetch_live_card_content(message_id):
    """Fetch the current interactive-card JSON from Lark as a last-resort copy.

    This covers freshly posted/manual-scan cards when the post succeeded but the
    local checkpoint write did not complete.  We only accept real CardKit JSON
    returned by the message API; compact text renderings are intentionally not
    used because they cannot be patched safely.
    """
    if not message_id:
        return None

    def make_request():
        token = _get_bot_access_token()
        if not token:
            raise RuntimeError("Could not get bot access token")
        return urllib.request.Request(
            f"https://open.larksuite.com/open-apis/im/v1/messages/{message_id}",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )

    try:
        data = request_json_with_auth_retry(make_request, timeout=30, context=f"fetch_live_card_content:{message_id}")
        items = (data.get("data") or {}).get("items") or []
        if not items:
            return None
        content = ((items[0].get("body") or {}).get("content")) or ""
        card = json.loads(content) if content else None
        if isinstance(card, dict) and isinstance(card.get("elements"), list):
            return card
    except Exception as exc:
        print(f"fetch_live_card_content: could not fetch {message_id}: {exc!r}", flush=True)
    return None


def load_card_for_message(message_id, region=None, upc=None, isrc=None, tracker_row=None, ref_id=None, date=None):
    """Load the exact persisted card for a message, fetch it live, or rebuild it from the tracker.

    Never falls back to last_card.json: that is a different claim's card and
    patching it corrupts the clicked card (B8). If neither the persisted card
    nor a tracker-row reconstruction is available, raise a clear error so the
    caller can surface a toast instead of silently patching wrong data.
    """
    card = load_posted_card(message_id)
    if card:
        return card
    card = fetch_live_card_content(message_id)
    if card:
        try:
            ra.save_posted_card(message_id, card)
        except Exception as exc:
            print(f"load_card_for_message: could not persist live card {message_id}: {exc!r}", flush=True)
        return card
    card = reconstruct_card_from_tracker(
        message_id, region=region, upc=upc, isrc=isrc, tracker_row=tracker_row, ref_id=ref_id, date=date
    )
    if card:
        return card
    raise RuntimeError(
        f"No persisted card for message {message_id} and tracker reconstruction failed; "
        f"refusing to patch an unrelated last_card.json."
    )


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

    # C8: Append the "**Status: …**" audit line to the case-summary element (the
    # one containing "Artist(s)"), NOT element 0. After build_card_with_countdown
    # runs, element 0 is the countdown badge, so the old `elements[0]` assumption
    # dropped the status line into the wrong element. Locate the summary element
    # by content and fall back to the first text element if it cannot be found.
    summary_el = None
    for el in card.get("elements", []):
        text = el.get("text") if isinstance(el, dict) else None
        content = text.get("content") if isinstance(text, dict) else None
        if isinstance(content, str) and "Artist(s)" in content:
            summary_el = el
            break
    if summary_el is None:
        for el in card.get("elements", []):
            text = el.get("text") if isinstance(el, dict) else None
            if isinstance(text, dict) and isinstance(text.get("content"), str):
                summary_el = el
                break
    if summary_el is not None:
        lines = summary_el["text"]["content"].split("\n")
        lines = [line for line in lines if not line.startswith("**Status:")]
        lines.append(audit_line)
        summary_el["text"]["content"] = "\n".join(lines)

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
    try:
        raw_values = extract_sheet_values(_sheet_api("GET", sheet_url, sheet_id, "A1:Z2000"))
        rows = []
        for row in raw_values:
            row = list(row or [])
            if len(row) < 26:
                row.extend([""] * (26 - len(row)))
            rows.append(row[:26])
        return rows
    except Exception as exc:
        print(f"Sheet read via persisted user OAuth failed; falling back to lark-cli legacy path: {exc!r}", flush=True)
    try:
        return _read_sheet_values_cli(sheet_url, sheet_id)
    except Exception as exc:
        print(f"Sheet read via lark-cli fallback failed: {exc!r}", flush=True)
        return []


def _find_tracker_row(values, *, message_id=None, upc=None, isrc=None, ref_id=None, date=None, tracker_row=None):
    """Locate the tracker row for a claim using a strict match-priority hierarchy.

    J4 — rows are matched by claim identity in this order, strongest first:

      1. ``message_id`` — card message id, matched against column P
         (Lark Message ID) OR column S (Card Message ID). Strongest key.
      2. ``ref_code``   — column U, when the callback carries a ``ref_id``
         (Spotify claims only; column U is blank for non-Spotify claims).
      3. ``date + upc`` — columns O (Date Received, date-part only) + A (UPC),
         when no ref_code is available and a ``date`` was supplied.
      4. most-recent UPC — if several rows still match on UPC alone, pick the one
         with the latest Date Received, tie-broken by the highest row number.
         Never silently defaults to the first/topmost row.

    An explicit ``tracker_row`` hint still wins when it matches a supplied stable
    id. A plain ISRC lookup is kept as a last-resort fallback for legacy
    non-UPC claims. Every successful match logs ``matched_by: <tier>`` so the
    chosen tier is auditable. Returns ``(row_num, match_reason, header_index)``.
    """
    if not values:
        return None, None, {}

    headers = [_norm(v) for v in values[0]]
    header_index = {name: idx for idx, name in enumerate(headers) if name}
    message_idx = header_index.get(MESSAGE_ID_COL)          # column P
    card_msg_idx = header_index.get(CARD_MSG_ID_COL_NAME)   # column S
    upc_idx = header_index.get(UPC_COL_NAME)                # column A
    isrc_idx = header_index.get(ISRC_COL_NAME)
    ref_idx = header_index.get(REF_CODE_COL_NAME)           # column U
    date_idx = header_index.get(DATE_RECEIVED_COL_NAME)     # column O

    def _get(row, idx):
        return _norm(row[idx]) if idx is not None and len(row) > idx else ""

    def _msg_cell(row):
        # The group-card message id is written to both column P and column S, so
        # a click may match on either.
        return _get(row, message_idx) or _get(row, card_msg_idx)

    def _date_only(raw):
        raw = _norm(raw)
        if not raw:
            return ""
        # Keep only the date part (before any space or 'T' time separator).
        return raw.split(" ")[0].split("T")[0]

    def _date_sort_key(raw):
        d = _date_only(raw)
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(d, fmt)
            except ValueError:
                continue
        # Unparseable/blank dates sort oldest so a parseable, more-recent row wins.
        return datetime.min

    def row_identity(row):
        return _msg_cell(row), _get(row, upc_idx), _get(row, isrc_idx)

    def row_matches_supplied_identity(row):
        """Return True when the hinted row matches at least one supplied stable id.

        Older cards can carry stale tracker_row values. Trusting the row hint
        blindly can write a callback to the wrong row, especially after a row was
        manually inserted near the top. The hint is only authoritative when it
        matches the message_id, UPC, or ISRC present in the callback payload.
        """
        msg_cell, upc_cell, isrc_cell = row_identity(row)
        expected = [
            ("message_id", _norm(message_id), msg_cell),
            ("upc", _norm(upc), upc_cell),
            ("isrc", _norm(isrc), isrc_cell),
        ]
        supplied = [(name, want, got) for name, want, got in expected if want]
        if not supplied:
            return True
        return any(got and got == want for _name, want, got in supplied)

    row_num = None
    match_reason = None

    # Explicit tracker_row hint — honoured only when it still matches a supplied
    # stable id (older cards can carry a stale row number).
    if tracker_row not in (None, ""):
        try:
            candidate_row = int(tracker_row)
            if 2 <= candidate_row <= len(values):
                candidate = values[candidate_row - 1]
                if row_matches_supplied_identity(candidate):
                    row_num = candidate_row
                    match_reason = "tracker_row"
                else:
                    print(
                        "Ignoring stale tracker_row hint:",
                        json.dumps({"tracker_row": tracker_row, "message_id": message_id, "upc": upc, "isrc": isrc}, ensure_ascii=False),
                        flush=True,
                    )
        except (TypeError, ValueError):
            print(f"Invalid tracker_row hint: {tracker_row!r}")

    # Tier 1 — card message_id (column P or S). Strongest key.
    if not row_num and message_id:
        want = _norm(message_id)
        if want:
            for idx, row in enumerate(values[1:], start=2):
                if _msg_cell(row) == want:
                    row_num, match_reason = idx, "message_id"
                    break

    # Tier 2 — ref_code (column U). Spotify claims only; column U is blank for
    # non-Spotify claims, so this never matches a non-Spotify row.
    if not row_num and ref_id and ref_idx is not None:
        want = _norm(ref_id)
        if want:
            for idx, row in enumerate(values[1:], start=2):
                if _get(row, ref_idx) == want:
                    row_num, match_reason = idx, "ref_code"
                    break

    # Tiers 3 & 4 — UPC-based matching. Collect every row sharing the UPC, then
    # narrow by Date Received (tier 3) or fall back to the most-recent row
    # (tier 4). Never default to the first/topmost row.
    if not row_num and upc and upc_idx is not None:
        want_upc = _norm(upc)
        upc_rows = (
            [idx for idx, row in enumerate(values[1:], start=2) if _get(row, upc_idx) == want_upc]
            if want_upc else []
        )
        if upc_rows:
            want_date = _date_only(date) if date else ""
            date_rows = [
                rn for rn in upc_rows
                if want_date and _date_only(_get(values[rn - 1], date_idx)) == want_date
            ]
            if date_rows:
                # Tier 3: exact Date Received + UPC. If still multiple, take the
                # highest (latest-appended) row.
                row_num = max(date_rows)
                match_reason = "date+upc"
            else:
                # Tier 4: most-recent by Date Received, tie-break by highest row.
                row_num = max(
                    upc_rows,
                    key=lambda rn: (_date_sort_key(_get(values[rn - 1], date_idx)), rn),
                )
                match_reason = "most_recent_upc"

    # Last-resort fallback — ISRC (legacy non-UPC claims). Most-recent if several
    # rows match, never the first/topmost.
    if not row_num and isrc and isrc_idx is not None:
        want_isrc = _norm(isrc)
        isrc_rows = (
            [idx for idx, row in enumerate(values[1:], start=2) if _get(row, isrc_idx) == want_isrc]
            if want_isrc else []
        )
        if isrc_rows:
            row_num = max(
                isrc_rows,
                key=lambda rn: (_date_sort_key(_get(values[rn - 1], date_idx)), rn),
            )
            match_reason = "isrc"

    if row_num:
        print(f"matched_by: {match_reason} (row {row_num})", flush=True)
    return row_num, match_reason, header_index



def update_sheet_status(message_id, status, upc=None, isrc=None, region=None, tracker_row=None, ref_id=None, date=None):
    sheet_url, sheet_id = _tracker_config(region)
    values = read_sheet_values(region=region)
    if not values:
        print("Sheet row not found: sheet is empty or unreadable")
        return False

    row_num, match_reason, header_index = _find_tracker_row(
        values,
        message_id=message_id,
        upc=upc,
        isrc=isrc,
        ref_id=ref_id,
        date=date,
        tracker_row=tracker_row,
    )
    status_idx = header_index.get(STATUS_COL_NAME)
    message_idx = header_index.get(MESSAGE_ID_COL)

    if status_idx is None:
        # Do not silently write to a fixed fallback letter: if the tracker
        # header layout changed, blindly writing to a hardcoded column can
        # corrupt an unrelated column. Fail loudly instead (B1).
        raise RuntimeError(
            f"Tracker is missing the {STATUS_COL_NAME!r} column; cannot write status. "
            f"Headers: {[_norm(v) for v in values[0]]}"
        )
    if message_idx is None:
        print("Sheet is missing Lark Message ID column; headers:", [_norm(v) for v in values[0]])

    if not row_num:
        print("Sheet row not found for", json.dumps({"message_id": message_id, "upc": upc, "isrc": isrc}, ensure_ascii=False))
        return False

    updates = [(_col_letter(status_idx), str(status if status is not None else ""))]
    if message_idx is not None:
        current_msg = _norm(values[row_num - 1][message_idx]) if len(values[row_num - 1]) > message_idx else ""
        message_id_str = str(message_id if message_id is not None else "")
        # J1: Never blank column P (Lark Message ID). A DM-card Status write-back
        # can arrive with an empty message_id (e.g. a `/card`-generated card that
        # carried no group-card message id). Without the `message_id_str` guard the
        # sync would write "" into column P and erase the real group-card message
        # id, breaking every later button patch/lookup for that row.
        if message_id_str and current_msg != _norm(message_id_str):
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


    """Resolve a column letter by header name.

    Prefer raising a clear error when the header lookup fails rather than
    silently writing to a hardcoded fallback letter, which can corrupt an
    unrelated column if the tracker layout changed (B1). The ``fallback_letter``
    argument is retained only for signature compatibility and is no longer used
    for a silent write.
    """


    values = read_sheet_values(region=region)


    if values:


        headers = [_norm(v) for v in values[0]]


        for idx, name in enumerate(headers):


            if name == header_name:


                return _col_letter(idx)

        raise TrackerColumnMissingError(
            f"Tracker is missing the {header_name!r} column; refusing to write. "
            f"Headers: {headers}"
        )

    raise TrackerReadError(
        f"Tracker is empty or unreadable; cannot resolve column {header_name!r}."
    )











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
    # H1.4: The Email Status column letter is stable ("T" per the tracker
    # schema). If the READ itself failed (empty/unreadable — typically a stale
    # daemon JWT), a recorded status in the known-schema column beats crashing
    # the whole reply flow and mis-blaming draft creation. This fallback is
    # ONLY taken on a read failure (TrackerReadError). It must NOT be taken when
    # the sheet was read fine but the header is absent (TrackerColumnMissingError
    # — a real layout change), because blind-writing there could corrupt an
    # unrelated column (B1).
    try:
        col = _find_col_letter(EMAIL_STATUS_COL_NAME, EMAIL_STATUS_COL, region=region)
    except TrackerReadError as exc:
        col = EMAIL_STATUS_COL
        print(
            f"⚠️ update_sheet_email_status: tracker READ failed ({exc}); "
            f"falling back to stable Email Status column {EMAIL_STATUS_COL!r}{row_num} "
            f"— recording status in the known-schema column instead of crashing.",
            flush=True,
        )
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

    card = update_card_state(
        load_card_for_message(
            message_id,
            region=payload.get("region"),
            upc=payload.get("upc"),
            isrc=payload.get("isrc"),
            tracker_row=payload.get("tracker_row"),
            ref_id=payload.get("ref_id"),
            date=payload.get("date") or payload.get("detected_at"),
        ),
        status,
        message_id,
    )
    Path("copyright_alert/last_card_callback.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))

    patched = patch_message(message_id, card)
    sheet_ok = update_sheet_status(
        message_id,
        status,
        upc=payload.get("upc"),
        isrc=payload.get("isrc"),
        region=payload.get("region"),
        tracker_row=payload.get("tracker_row"),
        ref_id=payload.get("ref_id"),
        date=payload.get("date") or payload.get("detected_at"),
    )
    print(json.dumps({"patched": patched, "sheet_updated": sheet_ok, "status": status, "message_id": message_id, "region": payload.get("region")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
