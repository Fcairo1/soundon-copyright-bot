#!/usr/bin/env python3
"""
copyright_alert/tag_managers.py

PART 2 — every-2-workdays AM tagging script.

Reads the copyright tracker sheet, finds rows that are still pending
  (Status == "🔍 Investigating" OR Status empty)  AND  "Admin Action Taken" empty
and posts ONE message to the alert group (oc_fd2e43d6451f8d87adb4cd4ceefa7816)
that @-mentions each responsible manager (Label Manager + BD) with the UPCs they
are accountable for, grouped per person, e.g.:

    @Mariana Vieira: UPCs 796728442269, 047752352704 — please provide an update
    on these infringement claims

Manager usernames (needed for real Lark @mentions) are resolved from the Aeolus
Song Dimension dataset by UPC/ISRC (same source run_alert.py uses to build the
card mentions). If Aeolus has no row for a UPC, we fall back to deriving a
username from the sheet's display-name columns.

Usage:
    python3 copyright_alert/tag_managers.py            # post to the group
    python3 copyright_alert/tag_managers.py --dry-run  # print, do not post
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from copyright_alert.run_alert import (  # noqa: E402
    TARGET_CHAT_ID,
    TRACKER_SHEET_URL,
    TRACKER_SHEET_ID,
    parse_lark_json,
    query_aeolus,
    _parse_people_list,
    _display_name_from_username,
    _get_bot_access_token,
)
from copyright_alert.manager_exclusions import (
    HARDCODED_GLOBAL_EXCLUSIONS,
    filter_manager_pairs,
)
from copyright_alert.upc_exclusions import is_upc_excluded

ADMIN_ACTION_HEADER = "Admin Action Taken"
PENDING_STATUSES = {
    "",
    "Investigating",
    "Disputing",
}
MENTION_DOMAIN = "bytedance.com"
ADMIN_UPC_URL_TEMPLATE = (
    "https://sg-musician-admin.bytedance.net/avenue/content/album/new"
    "?currentPage=1&pageSize=10&showFields=upc&upc={upc}"
)
# Permanently skip these managers from ALL alert tagging (defence in depth on
# top of manager_exclusions.json's __global__ list).
HARD_SKIP_USERNAMES = {u.lower() for u in HARDCODED_GLOBAL_EXCLUSIONS}

# SLA configuration: managers have 5 workdays from Date Received to act.
SLA_WORKDAYS = 5
BRT_OFFSET = timedelta(hours=-3)


def today_brt() -> date:
    """Today in São Paulo (BRT, UTC-3) — used as the reference for SLA math."""
    return (datetime.now(timezone.utc) + BRT_OFFSET).date()


def _parse_date_received(raw: str):
    """Parse the various Date Received formats found in the BR tracker.

    Examples seen on the sheet:
      • '2026-06-12T21:14:16Z'  (ISO with timezone)
      • '2026-06-16 06:49'      (naive, treated as BRT)
      • '2026-06-15 23:50'
    Returns a `date` in BRT, or None if unparseable.
    """
    text = (raw or "").strip()
    if not text:
        return None
    # ISO 8601 with Z
    try:
        if text.endswith("Z"):
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return (dt + BRT_OFFSET).date()
    except ValueError:
        pass
    # Naive 'YYYY-MM-DD HH:MM[:SS]' — treat as BRT local
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Fallback: pull the leading YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _add_workdays(start: date, n: int) -> date:
    """Return the date that is `n` workdays (Mon–Fri) after `start`.

    `start` itself is workday 0; if `start` is Mon and n=5, returns the
    following Mon. Holidays are not modeled (the tracker does not encode them).
    """
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Mon..Fri
            added += 1
    return d


def _net_workdays(a: date, b: date) -> int:
    """Workdays from `a` (exclusive) to `b` (inclusive), Mon–Fri.

    Positive when b is after a; negative when b is before a (i.e. overdue).
    """
    if a == b:
        return 0
    step = 1 if b > a else -1
    cur = a
    count = 0
    while cur != b:
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            count += step
    return count


# ── Unified reply deadline (C1) ──────────────────────────────────────────────
# The group-card countdown, the DM action card, and the manager SLA must all
# agree: 5 BUSINESS days in BRT (UTC-3). This is the single source of truth;
# other modules import REPLY_DEADLINE_WORKDAYS / business_days_remaining_brt
# instead of defining their own calendar/business-day constants.
REPLY_DEADLINE_WORKDAYS = SLA_WORKDAYS


def business_days_remaining_brt(detected) -> int:
    """Business days remaining until the 5-workday BRT reply deadline (C1).

    ``detected`` may be a ``date``, ``datetime``, or a raw Date-Received string.
    The deadline is ``REPLY_DEADLINE_WORKDAYS`` (=5) Mon–Fri workdays after the
    detected date; remaining is measured from today in BRT.
    Returns: positive = days left, 0 = due today, negative = overdue. If the
    detected date is unknown, returns the full window.
    """
    if isinstance(detected, str):
        detected = _parse_date_received(detected)
    elif isinstance(detected, datetime):
        detected = detected.date()
    if not detected:
        return REPLY_DEADLINE_WORKDAYS
    deadline = _add_workdays(detected, REPLY_DEADLINE_WORKDAYS)
    return _net_workdays(today_brt(), deadline)


def _format_sla_suffix(received_raw: str, today: date) -> str:
    """Build the trailing piece of a UPC line: '⏳ X days remaining' / '🔴 overdue'."""
    received = _parse_date_received(received_raw)
    if not received:
        return "⏳ deadline unknown"
    deadline = _add_workdays(received, SLA_WORKDAYS)
    remaining = _net_workdays(today, deadline)
    if remaining > 0:
        unit = "day" if remaining == 1 else "days"
        return f"⏳ {remaining} {unit} remaining"
    if remaining == 0:
        return "⏳ due today"
    overdue = -remaining
    unit = "day" if overdue == 1 else "days"
    return f"🔴 {overdue} {unit} overdue"


def log(msg=""):
    print(msg, flush=True)


def _norm(v):
    return str(v if v is not None else "").strip()


def _normalized_status(v):
    text = _norm(v)
    if not text:
        return ""
    text = text.replace("⚖️", "").replace("⚖", "")
    text = text.replace("🔍", "")
    text = text.replace("🔴", "")
    return " ".join(text.split()).strip()


def _admin_action_has_real_value(value: str) -> bool:
    normalized = _norm(value).casefold()
    return bool(normalized) and normalized != "no"


def _is_pending_row(status: str, admin_action: str) -> bool:
    normalized = _normalized_status(status).casefold()
    if normalized == "resolved":
        return False
    if normalized == "confirm takedown":
        return not _admin_action_has_real_value(admin_action)
    return normalized in {s.casefold() for s in PENDING_STATUSES}


def _cell(row, i):
    return _norm(row[i]) if i is not None and len(row) > i else ""


def read_sheet_values(rng="A:Z"):
    cmd = [
        "lark-cli", "sheets", "+read", "--url", TRACKER_SHEET_URL,
        "--sheet-id", TRACKER_SHEET_ID, "--range", rng, "--format", "json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        log(f"✗ Sheet read failed: {(res.stdout + res.stderr)[:400]}")
        return []
    parsed = parse_lark_json(res.stdout)
    if not parsed:
        return []
    data_obj = parsed.get("data") or {}
    value_range = data_obj.get("valueRange") or {}
    if value_range.get("values") is not None:
        return value_range.get("values") or []
    ranges = data_obj.get("ranges") or []
    if ranges and ranges[0].get("cells") is not None:
        return [[(cell or {}).get("value") for cell in row] for row in (ranges[0].get("cells") or [])]
    return []


def _username_from_display(display):
    """Best-effort fallback: 'Mariana Vieira' -> 'mariana.vieira'."""
    d = _norm(display)
    if not d or d.upper() == "N/A":
        return ""
    return ".".join(p for p in d.lower().split() if p)


def resolve_managers_for_row(row, idx):
    """Return list of (username, display_name) for a row.

    Primary source: Aeolus (bd_manager_list + operation_manager_list usernames).
    Fallback: derive from the sheet's BD / Label Manager display-name columns.
    Applies the persistent per-label exclusion list when a Label UID is available.
    """
    upc = _cell(row, idx.get("UPC"))
    isrc = _cell(row, idx.get("ISRC"))
    label_uid = _cell(row, idx.get("UID"))

    usernames = []
    lookup_id = isrc if isrc and isrc != "N/A" else upc
    lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
    if lookup_id and lookup_id != "N/A":
        ar = query_aeolus(lookup_id, lookup_type) or {}
        if not label_uid:
            label_uid = str(ar.get("uid") or "").strip()
        usernames += _parse_people_list(ar.get("bd_manager_list"))
        usernames += _parse_people_list(ar.get("operation_manager_list"))

    if usernames:
        result, seen = [], set()
        for u in usernames:
            key = u.lower()
            if key in seen:
                continue
            if key in HARD_SKIP_USERNAMES:
                continue
            seen.add(key)
            result.append((u, _display_name_from_username(u)))
        return filter_manager_pairs(label_uid, result)

    # Fallback: sheet display-name columns
    result, seen = [], set()
    for col in ("BD", "Label Manager"):
        raw = _cell(row, idx.get(col))
        if not raw or raw.upper() == "N/A":
            continue
        for name in [p.strip() for p in raw.split(",") if p.strip()]:
            uname = _username_from_display(name)
            if not uname or uname in seen:
                continue
            if uname.lower() in HARD_SKIP_USERNAMES:
                continue
            seen.add(uname)
            result.append((uname, name))
    return filter_manager_pairs(label_uid, result)


def collect_pending(values):
    """Build per-manager UPC groups across all pending rows."""
    if not values or len(values) < 2:
        return {}, [], []

    headers = [_norm(h) for h in values[0]]
    idx = {h: i for i, h in enumerate(headers) if h}
    status_i = idx.get("Status")
    upc_i = idx.get("UPC")
    title_i = idx.get("Title")
    received_i = idx.get("Date Received")
    admin_i = idx.get(ADMIN_ACTION_HEADER)

    managers = {}        # username -> {"display": str, "items": [(upc, title, received), ...]}
    pending_rows = []
    no_manager_rows = []
    for r, row in enumerate(values[1:], start=2):
        if not any(_norm(c) for c in row):
            continue
        status = _cell(row, status_i)
        admin_done = _cell(row, admin_i)
        if not _is_pending_row(status, admin_done):
            continue
        upc = _cell(row, upc_i) or "N/A"
        if is_upc_excluded(upc):
            log(f"  Skipping excluded UPC {upc} on row {r}.")
            continue
        title = _cell(row, title_i) or "(untitled)"
        received = _cell(row, received_i)
        pending_rows.append({"row": r, "upc": upc, "title": title,
                             "received": received, "status": status or "(empty)"})
        log(f"  Pending row {r}: UPC {upc} title={title!r} received={received!r} "
            f"status={status or '(empty)'} — resolving managers …")
        people = resolve_managers_for_row(row, idx)
        if not people:
            log(f"    ⚠ No manager could be resolved for row {r} (UPC {upc}).")
            no_manager_rows.append({"row": r, "upc": upc, "title": title, "received": received})
        for uname, display in people:
            entry = managers.setdefault(uname, {"display": display, "items": []})
            triple = (upc, title, received)
            if triple not in entry["items"]:
                entry["items"].append(triple)
    return managers, pending_rows, no_manager_rows


def _md_escape(text: str) -> str:
    """Escape characters that would otherwise break a Lark markdown link label."""
    return _norm(text).replace("\\", "\\\\").replace("[", "(").replace("]", ")")


def build_tag_card(managers, no_manager_rows=None):
    """Build an interactive card that @-mentions each manager with their UPCs.

    Layout (per manager):
        @Manager Name — please provide an update on these infringement claims:
        • [UPC1](admin_url) — Title 1
        • [UPC2](admin_url) — Title 2
    """
    elements = [
        {"tag": "div", "text": {"tag": "lark_md",
         "content": "The following open infringement claims still need an update from their owners:"}},
        {"tag": "hr"},
    ]

    today = today_brt()
    no_manager_rows = no_manager_rows or []

    for uname, info in managers.items():
        mention = f'<at email="{uname}@{MENTION_DOMAIN}">{info["display"]}</at>'
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"{mention} — please provide an update on these infringement claims:"},
        })
        upc_lines = []
        for upc, title, received in info["items"]:
            url = ADMIN_UPC_URL_TEMPLATE.format(upc=upc)
            sla = _format_sla_suffix(received, today)
            upc_lines.append(f"• [{upc}]({url}) — {_md_escape(title)} — {sla}")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(upc_lines)},
        })

    if no_manager_rows:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**⚠️ No Manager Assigned**"},
        })
        no_manager_lines = []
        for item in no_manager_rows:
            upc = item.get("upc") or "N/A"
            title = item.get("title") or "(untitled)"
            received = item.get("received") or ""
            url = ADMIN_UPC_URL_TEMPLATE.format(upc=upc)
            sla = _format_sla_suffix(received, today)
            no_manager_lines.append(f"• [{upc}]({url}) — {_md_escape(title)} — {sla}")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(no_manager_lines)},
        })

    elements += [
        {"tag": "hr"},
        {"tag": "note", "elements": [
            {"tag": "lark_md", "content": f"[Open the tracker sheet]({TRACKER_SHEET_URL})"}
        ]},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "🔔 Infringement Claims — Update Requested"}
        },
        "elements": elements,
    }


def post_card_to_chat(card):
    return _post_card(card, TARGET_CHAT_ID, "chat_id")


def send_card_dm(card, email):
    return _post_card(card, email, "email")


def _post_card(card, receive_id, receive_id_type):
    token = _get_bot_access_token()
    if not token:
        log("✗ Could not obtain bot token.")
        return False, ""
    url = f"https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    body = json.dumps({
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode()
            parsed = json.loads(text)
            log(f"Post result ({receive_id_type}={receive_id}): HTTP {resp.status} code={parsed.get('code')} msg={parsed.get('msg')}")
            return parsed.get("code") == 0, ((parsed.get("data") or {}).get("message_id") or "")
    except urllib.error.HTTPError as e:
        log(f"Post HTTP error: {e.code} {e.read().decode()[:600]}")
        return False, ""


def main():
    dry_run = "--dry-run" in sys.argv
    dm_preview_email = None
    for i, a in enumerate(sys.argv):
        if a == "--dm-preview" and i + 1 < len(sys.argv):
            dm_preview_email = sys.argv[i + 1]
            break
        if a.startswith("--dm-preview="):
            dm_preview_email = a.split("=", 1)[1]
            break
    log(f"tag_managers — started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"(dry_run={dry_run}, dm_preview={dm_preview_email or '-'})")
    log(f"Tracker: {TRACKER_SHEET_URL}")
    log(f"Alert group: {TARGET_CHAT_ID}")

    values = read_sheet_values("A:Z")
    managers, pending_rows, no_manager_rows = collect_pending(values)

    log(f"\nPending rows: {len(pending_rows)} | Managers to tag: {len(managers)}")
    for uname, info in managers.items():
        upcs = ", ".join(item[0] for item in info["items"])
        log(f"  @{info['display']} ({uname}@{MENTION_DOMAIN}): UPCs {upcs}")

    log(f"No-manager pending rows: {len(no_manager_rows)}")
    for item in no_manager_rows:
        log(f"  ⚠ No manager assigned: UPC {item['upc']} — {item['title']}")

    if not managers and not no_manager_rows:
        log("\n✓ Nothing pending to tag. No message posted.")
        return {"pending_rows": len(pending_rows), "managers": 0, "no_manager_rows": 0, "posted": False}

    card = build_tag_card(managers, no_manager_rows)

    if dry_run:
        log("\n[DRY-RUN] Card that WOULD be posted:")
        log(json.dumps(card, ensure_ascii=False, indent=2))
        return {"pending_rows": len(pending_rows), "managers": len(managers),
                "no_manager_rows": len(no_manager_rows), "posted": False, "dry_run": True}

    if dm_preview_email:
        ok, msg_id = send_card_dm(card, dm_preview_email)
        log(f"\n{'✅ Sent' if ok else '✗ Failed to send'} DM preview to {dm_preview_email}. message_id={msg_id}")
        return {"pending_rows": len(pending_rows), "managers": len(managers),
                "no_manager_rows": len(no_manager_rows),
                "posted": ok, "message_id": msg_id, "dm_preview": dm_preview_email}

    ok, msg_id = post_card_to_chat(card)
    log(f"\n{'✅ Posted' if ok else '✗ Failed to post'} tagging message. message_id={msg_id}")
    return {"pending_rows": len(pending_rows), "managers": len(managers),
            "no_manager_rows": len(no_manager_rows), "posted": ok, "message_id": msg_id}


if __name__ == "__main__":
    main()
