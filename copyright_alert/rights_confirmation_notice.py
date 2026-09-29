#!/usr/bin/env python3
"""
copyright_alert/rights_confirmation_notice.py

Handler for Spotify "Rights Confirmation" notices — a third, distinct Spotify
email type alongside infringement claims (run_alert.py) and metadata/
misrepresentation notices (metadata_notice.py).

Spotify sends this when it questions whether SoundOn holds the rights to
DELIVER a release at all (not a third-party ownership claim, not a
misrepresentation flag). It comes from the SAME sender as real infringement
claims (infringement-claim-response@spotify.com), so sender alone cannot
distinguish it — confirmed live against the real inbox 2026-09-28. The
reliable signal is the subject prefix:

  "Possibly Infringing - Notification Warning ..."  -> real infringement claim
  "Takedown Notification - ..."                     -> metadata/misrepresentation notice
  "Content Takedown - <Label> - Claim <N> - ..."     -> THIS type (rare: ~2 in 50
                                                         recent claim-response emails)

Handling, per the user's explicit spec:
  * A new "Rights Confirmation" tab on EACH of the 3 regional tracker
    workbooks (same precedent as metadata_notice.py's "Metadata Corrections"
    tab — same workbooks, reused via metadata_notice._METADATA_TRACKERS).
  * Headers: UPC, User ID, User Source, BD, Label Manager, Status,
    Date Received, Card Message ID, Email Status.
  * Status: "We have rights" / "We do not have rights" / "Investigating"
    (new rows default to "Investigating").
  * A GROUP card posted to the SAME group as normal infringement claims
    (run_alert.TARGET_CHAT_ID), tagging BD + Label Manager, asking for proof
    of rights/authorization to distribute. Distinct header color (blue) from
    the normal claim card (red).
  * A private DM action card to the region's product-ops owner (same
    _ops_context_for_region routing as metadata_notice.py) with 2 buttons:
    "✅ We have rights" (opens a threaded reply draft containing Spotify's own
    required perjury statement, pre-filled with the real spotify: URI, plus
    an optional free-text field for the explanation/label contact details
    Spotify's own template requires) and "🚫 We do not have rights" (a
    threaded reply draft acknowledging the takedown, no contest).
  * Re-sent daily to product ops until the tracker row's Email Status is
    non-blank (mirrors metadata_notice.py's daily-resend-until-actioned loop,
    but gated on the sheet's own Email Status column per the user's spec,
    not a separate "resolved" flag).

Only handled as a rights-confirmation case when the UPC's Aeolus
source_type_name is "AP" or "A&R" (ELIGIBLE_SOURCE_TYPES below) — any other
source falls straight through to the normal infringement-claim flow in
daily_workflow.py instead, per explicit user instruction.

Rights-confirmation rows never count toward infringement-claim totals: they
live in their own "Rights Confirmation" tab/sheet_id, not the main claims
tracker tab the dashboard/claim counts read from — same isolation already
relied on for Metadata Corrections. An ineligible-source case that falls
through to the normal claim flow legitimately DOES count as a claim there,
which is the intended default when this specialized handling doesn't apply.

The existing backlog (2 real emails already misclassified as normal
infringement claims, per live inbox check 2026-09-28) is explicitly OUT of
scope per the user — this only applies going forward.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone

from copyright_alert.metadata_notice import (
    _METADATA_TRACKERS as _RIGHTS_TRACKERS,
    _run_lark_sheets,
    parse_metadata_notice,
)
from copyright_alert.run_alert import (
    RUNTIME_DIR,
    _canonical_alert_region,
    _display_name_from_username,
    _mention_people,
    _ops_context_for_region,
    _parse_people_list,
    labeled_value,
    patch_card_message,
    post_card,
    query_aeolus,
)
from copyright_alert.spotify_reply import _context_line, _ref_line, _send_reply, _wrap_html
from copyright_alert.state_io import update_json_state

# ── Constants ────────────────────────────────────────────────────────────────
STATE_FILE = str(RUNTIME_DIR / "rights_confirmation_state.json")
BRT = timezone(timedelta(hours=-3))

CALLBACK_ACTION = "rights_confirmation_reply"

RIGHTS_CONFIRMATION_SHEET_NAME = "Rights Confirmation"
RIGHTS_CONFIRMATION_HEADERS = [
    "UPC", "User ID", "User Source", "BD", "Label Manager",
    "Status", "Date Received", "Card Message ID", "Email Status",
]

STATUS_HAVE_RIGHTS = "We have rights"
STATUS_NO_RIGHTS = "We do not have rights"
STATUS_INVESTIGATING = "Investigating"

# Only handle a rights-confirmation notice as such when the UPC's Aeolus
# source_type_name (the same "Source" field shown on the normal claims
# tracker) is one of these — per explicit user instruction. Anything else
# falls through to the normal infringement-claim flow instead.
ELIGIBLE_SOURCE_TYPES = {"AP", "A&R"}

# Subject prefixes for the two OTHER Spotify claim-response email shapes, so
# this detector can positively exclude them rather than rely on body text
# alone (confirmed live against the real inbox: all 3 types share a sender).
_INFRINGEMENT_SUBJECT_PREFIX = re.compile(r"(?i)^\s*(re:\s*)?possibly infringing")
_METADATA_SUBJECT_PREFIX = re.compile(r"(?i)^\s*(re:\s*)?takedown notification")
_RIGHTS_SUBJECT_PREFIX = re.compile(r"(?i)^\s*(re:\s*)?content takedown\b")

_ADMIN_ALBUM_URL = (
    "https://sg-musician-admin.bytedance.net/avenue/content/album/new"
    "?currentPage=1&pageSize=10&showFields=upc&upc={upc}"
)


def _today_brt() -> str:
    return datetime.now(BRT).strftime("%Y-%m-%d")


def _now_brt_iso() -> str:
    return datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S %Z")


# ── Detection ────────────────────────────────────────────────────────────────
def is_rights_confirmation_notice(body, subject="", meta=None) -> bool:
    """Return True for a Spotify "rights to deliver" confirmation notice.

    All of:
      * Subject starts with "Content Takedown -" (the one prefix distinct
        from the other two Spotify claim-response email shapes).
      * Body asks us to confirm "necessary rights to deliver/post the
        content" (Spotify's own phrasing for this type).
      * NOT the metadata/misrepresentation subject shape (defensive — the
        caller should already route metadata notices out first).
    """
    subject = subject or ""
    if not _RIGHTS_SUBJECT_PREFIX.match(subject.strip()):
        return False
    if _METADATA_SUBJECT_PREFIX.match(subject.strip()) or _INFRINGEMENT_SUBJECT_PREFIX.match(subject.strip()):
        return False
    text = (body or "").lower()
    return ("necessary rights to deliver" in text) or ("necessary rights to post the content" in text)


_SPOTIFY_URI_RE = re.compile(r"spotify:(?:track|album|artist):[A-Za-z0-9]+")


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_rights_confirmation_notice(body, subject="", meta=None) -> dict:
    """Extract fields — reuses metadata_notice's parser for the shared shape
    (UPC/artist/title/spotify_uri/ref_id/date_received/notice_body), then adds
    the label name and Spotify claim number specific to this template.

    spotify_uri is always re-derived directly with a bounded regex rather than
    trusted from parse_metadata_notice(): this email's real body (confirmed
    live) has no line breaks at all, and labeled_value()'s stop-boundaries are
    all newline-anchored, so it falls through to capturing to end-of-string.
    Harmless for metadata notices (display-only field), but this value lands
    directly in the perjury statement of our actual reply to Spotify, so it
    must never be wrong.
    """
    fields = parse_metadata_notice(body, subject, meta)
    uri_match = _SPOTIFY_URI_RE.search(body or "")
    fields["spotify_uri"] = uri_match.group(0) if uri_match else "N/A"
    label = labeled_value(body or "", "Label Name")
    if label != "N/A":
        fields["label"] = label
    claim_match = re.search(r"Claim\s+(\d+)", subject or "")
    fields["claim_number"] = claim_match.group(1) if claim_match else "N/A"
    return fields


def notice_key(fields: dict) -> str:
    ref_id = str(fields.get("ref_id", "") or "").strip()
    if ref_id and ref_id != "N/A":
        return ref_id
    claim_number = str(fields.get("claim_number", "") or "").strip()
    upc = str(fields.get("upc", "") or "").strip()
    if claim_number and claim_number != "N/A":
        return f"claim:{claim_number}"
    return f"upc:{upc}" if upc and upc != "N/A" else f"raw:{hash(fields.get('subject', ''))}"


# ── Tracker sheet I/O (mirrors metadata_notice.py's sheet helpers) ───────────
def _rights_tracker_url(region: str) -> str:
    return _RIGHTS_TRACKERS.get(str(region or "").upper(), _RIGHTS_TRACKERS["BR"])


def _ensure_rights_confirmation_sheet(region: str) -> str:
    tracker_url = _rights_tracker_url(region)
    info = _run_lark_sheets(["+workbook-info", "--url", tracker_url])
    for sheet in (info.get("data") or {}).get("sheets") or []:
        if sheet.get("sheet_name") == RIGHTS_CONFIRMATION_SHEET_NAME:
            return sheet.get("sheet_id") or ""

    created = _run_lark_sheets([
        "+sheet-create", "--url", tracker_url, "--title", RIGHTS_CONFIRMATION_SHEET_NAME,
        "--row-count", "200", "--col-count", str(len(RIGHTS_CONFIRMATION_HEADERS)),
    ])
    sheet_id = str((created.get("data") or {}).get("sheet_id") or "")
    _run_lark_sheets([
        "+csv-put", "--url", tracker_url, "--sheet-id", sheet_id, "--start-cell", "A1",
        "--csv", ",".join(RIGHTS_CONFIRMATION_HEADERS) + "\n",
    ])
    return sheet_id


def _read_rights_tracker_rows(region: str):
    tracker_url = _rights_tracker_url(region)
    sheet_id = _ensure_rights_confirmation_sheet(region)
    if not sheet_id:
        raise RuntimeError(f"Missing {RIGHTS_CONFIRMATION_SHEET_NAME} sheet id for {region}")
    result = _run_lark_sheets([
        "+csv-get", "--url", tracker_url, "--sheet-id", sheet_id, "--include-row-prefix=false",
    ])
    csv_text = (result.get("data") or {}).get("annotated_csv") or ""
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return [], [], sheet_id
    header = [h.strip() for h in rows[0]]
    data_rows = [r for r in rows[1:] if any(c.strip() for c in r)]
    return header, data_rows, sheet_id


def _append_rights_confirmation_row(fields: dict, region: str, aeolus_row: dict) -> bool:
    tracker_url = _rights_tracker_url(region)
    sheet_id = _ensure_rights_confirmation_sheet(region)
    if not sheet_id:
        raise RuntimeError(f"Missing {RIGHTS_CONFIRMATION_SHEET_NAME} sheet id for {region}")

    bd = ", ".join(_display_name_from_username(p) for p in _parse_people_list(aeolus_row.get("bd_manager_list")))
    label_mgr = ", ".join(_display_name_from_username(p) for p in _parse_people_list(aeolus_row.get("operation_manager_list")))
    row = [
        fields.get("upc", "N/A"),
        str(aeolus_row.get("uid") or aeolus_row.get("user_id") or "N/A"),
        aeolus_row.get("source_type_name", "N/A"),
        bd,
        label_mgr,
        STATUS_INVESTIGATING,
        fields.get("date_received", "N/A"),
        "",  # Card Message ID — filled in by the caller once the group card posts
        "",  # Email Status
    ]
    payload = {"sheets": [{
        "name": RIGHTS_CONFIRMATION_SHEET_NAME,
        "mode": "append",
        "header": False,
        "columns": RIGHTS_CONFIRMATION_HEADERS,
        "data": [row],
        "dtypes": {h: "object" for h in RIGHTS_CONFIRMATION_HEADERS},
    }]}
    _run_lark_sheets(["+table-put", "--url", tracker_url, "--sheets", "-"], input_text=json.dumps(payload))
    return True


def _set_card_message_id(region: str, upc: str, card_message_id: str) -> bool:
    return _update_row_cell(region, upc, "Card Message ID", card_message_id)


def _set_email_status_and_status(region: str, upc: str, email_status: str, status: str) -> bool:
    ok1 = _update_row_cell(region, upc, "Email Status", email_status)
    ok2 = _update_row_cell(region, upc, "Status", status)
    return ok1 and ok2


def _update_row_cell(region: str, upc: str, header_name: str, value: str) -> bool:
    """Find the LAST row matching this UPC (most recent notice for it) and
    overwrite one cell by header name. Small, targeted PUT — never touches
    other columns/rows."""
    tracker_url = _rights_tracker_url(region)
    header, data_rows, sheet_id = _read_rights_tracker_rows(region)
    if not header:
        return False
    try:
        upc_i = header.index("UPC")
        col_i = header.index(header_name)
    except ValueError:
        return False
    target_row_num = None
    for offset, r in enumerate(data_rows):
        if (r[upc_i].strip() if len(r) > upc_i else "") == str(upc).strip():
            target_row_num = offset + 2  # +1 header, +1 for 1-indexing
    if target_row_num is None:
        return False
    from copyright_alert.metadata_notice import _column_letter
    col_letter = _column_letter(col_i)
    _run_lark_sheets([
        "+csv-put", "--url", tracker_url, "--sheet-id", sheet_id,
        "--start-cell", f"{col_letter}{target_row_num}", "--csv", value + "\n",
    ])
    return True


# ── State (day-based DM resend dedup — mirrors metadata_notice.py) ──────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("notices", {})
    return data


def get_notice(key: str) -> dict:
    return (_load_state().get("notices") or {}).get(key, {})


def find_notice_by_upc(upc: str) -> dict:
    """Most-recently-seen tracked notice for this UPC, or {} if none.

    Used by persistent_callback.py's on-demand `/card <UPC>` command so it can
    resend the RIGHT card type for a UPC that isn't a normal infringement
    claim (see also metadata_notice.find_notice_by_upc).
    """
    upc = str(upc or "").strip()
    if not upc:
        return {}
    best = {}
    for rec in (_load_state().get("notices") or {}).values():
        if str(rec.get("upc", "")).strip() != upc:
            continue
        if not best or rec.get("first_seen", "") >= best.get("first_seen", ""):
            best = rec
    return best


# ── Group card (posted to the SAME group as normal infringement claims) ─────
def build_rights_confirmation_group_card(fields: dict, region: str, aeolus_row: dict, *,
                                          resolved_status: str = "", thread_warning: str = "") -> dict:
    """BD/Label Manager can act straight from this group card — the same 2
    buttons as the DM card, since they're usually faster to respond than the
    regional ops DM owner (added per user request 2026-09-29)."""
    def v(val):
        return val if val and val != "N/A" else "N/A"

    upc_value = str(fields.get("upc", "N/A") or "N/A")
    upc_display = f"[{upc_value}]({_ADMIN_ALBUM_URL.format(upc=upc_value)})" if upc_value != "N/A" else "N/A"
    bd_mentions = _mention_people(aeolus_row.get("bd_manager_list"), region=region)
    label_mentions = _mention_people(aeolus_row.get("operation_manager_list"), region=region)
    tag_line = " ".join(m for m in (bd_mentions, label_mentions) if m) or "_(no BD/Label Manager on file)_"
    key = notice_key(fields)

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content":
            f"**{v(fields.get('title'))}**\nArtist(s): {v(fields.get('artist'))}\nLabel: {v(fields.get('label'))}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "Spotify is asking us to confirm SoundOn has the **necessary rights to deliver** this "
            "content — not a third-party ownership claim. Please provide proof that we can deliver "
            "this and that we have authorization to distribute the material."}},
        {"tag": "hr"},
        {
            "tag": "column_set", "flex_mode": "none", "background_style": "grey",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**UPC**\n{upc_display}"}}]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**Spotify Claim**\n{v(fields.get('claim_number'))}"}}]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**Received**\n{v(fields.get('date_received'))}"}}]},
            ],
        },
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": tag_line}},
    ]

    if resolved_status:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"✅ **{resolved_status}**"}})
        if thread_warning:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{thread_warning}**"}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            "**Explanation / label contact details** *(required if we have rights)*"}})
        elements.append({"tag": "input", "name": "explanation",
                          "placeholder": {"tag": "plain_text", "content": "why we have the rights + label contact info"}})
        elements.append({
            "tag": "action",
            "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "✅ We have rights"}, "type": "primary",
                 "value": {"action": CALLBACK_ACTION, "choice": "have_rights", "source": "group",
                           "key": key, "upc": upc_value, "region": region}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "🚫 We do not have rights"}, "type": "danger",
                 "value": {"action": CALLBACK_ACTION, "choice": "no_rights", "source": "group",
                           "key": key, "upc": upc_value, "region": region}},
            ],
        })

    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
        f"Spotify Content Protection · Region {region} · {v(fields.get('ref_id'))}"}]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "⚠️ Spotify Rights Confirmation Required"},
        },
        "elements": elements,
    }


def post_rights_confirmation_group_card(fields: dict, region: str, aeolus_row: dict):
    card = build_rights_confirmation_group_card(fields, region, aeolus_row)
    return post_card(card, aeolus_row=aeolus_row, upc=fields.get("upc"), context="rights_confirmation_group_card")


# ── DM action card (private, to the region's product-ops owner) ─────────────
def build_rights_confirmation_dm_card(fields: dict, region: str, *, resolved_status: str = "",
                                       thread_warning: str = "") -> dict:
    def v(val):
        return val if val and val != "N/A" else "N/A"

    upc_value = str(fields.get("upc", "N/A") or "N/A")
    key = notice_key(fields)

    if resolved_status:
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                f"**{v(fields.get('title'))}**\n✅ **{resolved_status}**"}},
        ]
        if thread_warning:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{thread_warning}**"}})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
            f"Region {region} · UPC {upc_value} · {v(fields.get('ref_id'))}"}]})
    else:
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                f"**{v(fields.get('title'))}**\nLabel: {v(fields.get('label'))}\n"
                "Spotify needs us to confirm we have the rights to deliver this content."}},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "If we have rights: Spotify requires an explanation and the label's contact details "
                "in the reply (added below), plus their required perjury statement (added automatically)."}},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "**Explanation / label contact details** *(required if we have rights)*"}},
            {"tag": "input", "name": "explanation",
             "placeholder": {"tag": "plain_text", "content": "why we have the rights + label contact info"}},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "✅ We have rights"}, "type": "primary",
                     "value": {"action": CALLBACK_ACTION, "choice": "have_rights", "source": "dm", "key": key, "upc": upc_value, "region": region}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "🚫 We do not have rights"}, "type": "danger",
                     "value": {"action": CALLBACK_ACTION, "choice": "no_rights", "source": "dm", "key": key, "upc": upc_value, "region": region}},
                ],
            },
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                f"Region {region} · {v(fields.get('ref_id'))} · Reminders re-send daily until answered"}]},
        ]

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "⚠️ Spotify Rights Confirmation"}},
        "elements": elements,
    }


def _send_to_ops(region: str, card: dict, *, log_context: str = "") -> dict:
    """Post an interactive card to the region's product-ops owner, trying
    chat_id / open_id / email in that order (same fallback order used
    everywhere else this project DMs a fixed regional contact)."""
    from copyright_alert.bot_runtime import _post_api
    from copyright_alert.dm_action_card import resolve_open_id

    ops = _ops_context_for_region(region)
    content = json.dumps(card, ensure_ascii=False)

    recipient_email = ops.get("ops_dm_email") or ""
    recipient_chat_id = ops.get("ops_dm_chat_id") or ""
    recipient_open_id = ops.get("ops_dm_open_id") or ""
    open_id = recipient_open_id or (resolve_open_id(recipient_email) if recipient_email else "")

    attempts = []
    if recipient_chat_id:
        attempts.append(("chat_id", recipient_chat_id))
    if open_id:
        attempts.append(("open_id", open_id))
    if recipient_email and "@" in recipient_email:
        attempts.append(("email", recipient_email))

    for id_type, rid in attempts:
        try:
            resp = _post_api(
                f"/im/v1/messages?receive_id_type={id_type}",
                {"receive_id": rid, "msg_type": "interactive", "content": content},
            )
            if resp.get("code") == 0:
                mid = ((resp.get("data") or {}).get("message_id")) or ""
                print(f"  ✓ Rights-confirmation DM sent via {id_type} ({rid}) → {mid} "
                      f"[region {region}{' · ' + log_context if log_context else ''}]", flush=True)
                return {"ok": True, "message_id": mid}
            print(f"  ✗ Rights-confirmation DM via {id_type} code={resp.get('code')} msg={resp.get('msg')}", flush=True)
        except Exception as exc:
            print(f"  ✗ Rights-confirmation DM via {id_type} failed: {exc!r}", flush=True)
    return {"ok": False, "message_id": ""}


def _send_rights_confirmation_dm(fields: dict, region: str) -> dict:
    card = build_rights_confirmation_dm_card(fields, region)
    return _send_to_ops(region, card, log_context=f"UPC {fields.get('upc')}")


# ── Reply drafts (threaded, via spotify_reply's generic _send_reply) ────────
def _reply_have_rights(source_email_message_id, upc, title, spotify_uri, explanation, ref_id):
    perjury = (
        f'I state under penalty of perjury that I have the necessary rights to post the content '
        f'found at {spotify_uri or "N/A"}, '
    )
    paragraphs = [p for p in (explanation, perjury, _context_line(upc, title), _ref_line(ref_id)) if p]
    body = _wrap_html(paragraphs)
    return _send_reply(source_email_message_id, body, "rights_confirmed",
                        fallback_subject=f"Spotify rights confirmation - UPC {upc}", upc=upc, ref_id=ref_id)


def _reply_no_rights(source_email_message_id, upc, title, ref_id):
    body = _wrap_html([
        "We do not contest this takedown. The content will remain removed from the service.",
        _context_line(upc, title), _ref_line(ref_id),
    ])
    return _send_reply(source_email_message_id, body, "rights_denied",
                        fallback_subject=f"Spotify rights confirmation - UPC {upc}", upc=upc, ref_id=ref_id)


# ── Main handler (called from the daily scan, right after metadata notices) ──
def handle_rights_confirmation_notice(body, subject="", meta=None, msg_id="") -> dict:
    fields = parse_rights_confirmation_notice(body, subject, meta)
    key = notice_key(fields)

    upc = str(fields.get("upc", "") or "").strip()
    aeolus_row = query_aeolus(upc) if upc and upc != "N/A" else {}
    region = _canonical_alert_region(aeolus_row=aeolus_row) if aeolus_row else "BR"

    source_type = str(aeolus_row.get("source_type_name", "") or "").strip()
    if source_type not in ELIGIBLE_SOURCE_TYPES:
        print(f"  • Rights-confirmation notice ineligible (source={source_type!r}, "
              f"needs AP/A&R) — falling through to normal claim handling ({key})", flush=True)
        return {"status": "ineligible_source", "key": key, "region": region, "source_type_name": source_type}

    existing = get_notice(key)
    if existing:
        def _touch(state):
            rec = state["notices"].get(key)
            if rec is not None:
                rec["last_seen"] = _now_brt_iso()
        update_json_state(STATE_FILE, _touch, default=lambda: {"notices": {}})
        print(f"  • Rights-confirmation notice already tracked ({key}, region {region}) — skipping duplicate", flush=True)
        return {"status": "already_tracked", "key": key, "region": region}

    tracker_ok = _append_rights_confirmation_row(fields, region, aeolus_row)
    group_ok, group_msg_id = post_rights_confirmation_group_card(fields, region, aeolus_row)
    if group_ok and group_msg_id:
        _set_card_message_id(region, upc, group_msg_id)
    dm = _send_rights_confirmation_dm(fields, region)

    def _insert(state):
        state["notices"][key] = {
            "key": key, "region": region, "fields": fields, "upc": upc,
            # Only the two fields build_rights_confirmation_group_card actually
            # needs, saved so the group card can be rebuilt/patched later
            # (e.g. when a manager resolves it from the group, or ops resolves
            # it from the DM and the group card needs to reflect that too).
            "aeolus_row": {
                "bd_manager_list": aeolus_row.get("bd_manager_list"),
                "operation_manager_list": aeolus_row.get("operation_manager_list"),
            },
            "source_email_message_id": msg_id or "",
            "first_seen": _now_brt_iso(), "last_seen": _now_brt_iso(),
            "last_dm_sent": _today_brt() if dm.get("ok") else "",
            "message_id": dm.get("message_id", ""),
            "group_message_id": group_msg_id,
            "tracker_row_written": tracker_ok,
            "answered": False,
        }
    update_json_state(STATE_FILE, _insert, default=lambda: {"notices": {}})

    print(f"  ✓ Rights-confirmation notice recorded ({key}, region {region}, "
          f"tracker ok={tracker_ok}, group ok={group_ok}, DM ok={dm.get('ok')})", flush=True)
    return {"status": "new", "key": key, "region": region, "tracker_ok": tracker_ok,
            "group_ok": group_ok, "dm_ok": dm.get("ok")}


# ── Daily re-send loop (gated on the tracker's own Email Status column) ─────
def resend_unanswered_notices() -> dict:
    """Re-send the DM for every notice whose tracker row still has a blank
    Email Status, at most once per calendar day (BRT) per notice."""
    state = _load_state()
    notices = state.get("notices") or {}
    today = _today_brt()
    summary = {"total": len(notices), "resent": 0, "skipped_today": 0,
               "skipped_answered": 0, "failed": 0}

    for key, rec in notices.items():
        if rec.get("answered"):
            summary["skipped_answered"] += 1
            continue
        region = rec.get("region", "BR")
        upc = rec.get("upc", "")
        header, data_rows, _ = _read_rights_tracker_rows(region)
        email_status = ""
        if header and "UPC" in header and "Email Status" in header:
            upc_i, es_i = header.index("UPC"), header.index("Email Status")
            for r in data_rows:
                if (r[upc_i].strip() if len(r) > upc_i else "") == str(upc).strip():
                    email_status = r[es_i].strip() if len(r) > es_i else ""
        if email_status:
            def _mark_answered(st, _key=key):
                rec2 = st["notices"].get(_key)
                if rec2 is not None:
                    rec2["answered"] = True
            update_json_state(STATE_FILE, _mark_answered, default=lambda: {"notices": {}})
            summary["skipped_answered"] += 1
            continue
        if rec.get("last_dm_sent") == today:
            summary["skipped_today"] += 1
            continue

        fields = rec.get("fields", {})
        dm = _send_rights_confirmation_dm(fields, region)

        def _update(st, _key=key, _dm=dm):
            r = st["notices"].get(_key)
            if r is None:
                return
            if _dm.get("ok"):
                r["last_dm_sent"] = today
                r["message_id"] = _dm.get("message_id", r.get("message_id", ""))
        update_json_state(STATE_FILE, _update, default=lambda: {"notices": {}})

        if dm.get("ok"):
            summary["resent"] += 1
        else:
            summary["failed"] += 1

    print(f"  Rights-confirmation re-send: {summary}", flush=True)
    return summary


# ── Button-click handler (called from the persistent callback daemon) ───────
def handle_reply_callback(value: dict, *, message_id: str = "", explanation: str = "") -> str:
    choice = str((value or {}).get("choice", "") or "").strip()
    key = str((value or {}).get("key", "") or "").strip()
    region = str((value or {}).get("region", "") or "BR").strip() or "BR"
    upc = str((value or {}).get("upc", "") or "").strip()
    source = str((value or {}).get("source", "") or "dm").strip()  # "dm" or "group"
    if not key:
        return "no key"

    rec = get_notice(key)
    fields = (rec or {}).get("fields", {})
    aeolus_row = (rec or {}).get("aeolus_row", {})
    source_email_message_id = (rec or {}).get("source_email_message_id", "")
    title = fields.get("title", "N/A")
    ref_id = fields.get("ref_id", "N/A")
    spotify_uri = fields.get("spotify_uri", "")

    if choice == "have_rights":
        result = _reply_have_rights(source_email_message_id, upc, title, spotify_uri, explanation, ref_id)
        status_value, email_status = STATUS_HAVE_RIGHTS, f"Sent ✅ – have rights – {_now_brt_iso()}"
        outcome_label = "We have rights"
    elif choice == "no_rights":
        result = _reply_no_rights(source_email_message_id, upc, title, ref_id)
        status_value, email_status = STATUS_NO_RIGHTS, f"Sent ✅ – no rights – {_now_brt_iso()}"
        outcome_label = "We do not have rights"
    else:
        return f"unknown choice: {choice!r}"

    _set_email_status_and_status(region, upc, email_status, status_value)

    # Surface a failed/degraded threading attempt loudly, same as the normal
    # infringement-claim reply flow — a silent standalone (non-threaded) draft
    # is exactly the recurring problem this was built to stop happening
    # invisibly.
    thread_warning = result.get("thread_warning", "") if isinstance(result, dict) else ""
    draft_url = result.get("send_preview_url", "") if isinstance(result, dict) else ""

    def _mark(state, _status=status_value, _warn=thread_warning):
        r = state["notices"].get(key)
        if r is not None:
            r["answered"] = True
            r["resolved_status"] = _status
            r["thread_warning"] = _warn
    update_json_state(STATE_FILE, _mark, default=lambda: {"notices": {}})

    # Patch whichever card was actually clicked (message_id points at that
    # one), then also patch the OTHER card so neither is left showing stale
    # buttons once the decision is made from either side.
    dm_message_id = (rec or {}).get("message_id", "")
    group_message_id = (rec or {}).get("group_message_id", "")
    if source == "group":
        group_message_id = message_id or group_message_id
    else:
        dm_message_id = message_id or dm_message_id

    if dm_message_id:
        try:
            patch_card_message(dm_message_id, build_rights_confirmation_dm_card(
                fields, region, resolved_status=status_value, thread_warning=thread_warning))
        except Exception as exc:
            print(f"  ⚠ Could not patch rights-confirmation DM card {dm_message_id}: {exc!r}", flush=True)
    if group_message_id:
        try:
            patch_card_message(group_message_id, build_rights_confirmation_group_card(
                fields, region, aeolus_row, resolved_status=status_value, thread_warning=thread_warning))
        except Exception as exc:
            print(f"  ⚠ Could not patch rights-confirmation group card {group_message_id}: {exc!r}", flush=True)

    # A manager resolving it from the group card is new information ops
    # hasn't seen yet (their own action-request DM is patched above, but
    # that's passive — proactively tell them a decision was already made).
    if source == "group":
        notice_card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": "green", "title": {"tag": "plain_text", "content": "✅ Rights confirmation resolved"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    f"**{title}** — UPC {upc}\nA manager confirmed in the group: **{outcome_label}**."}},
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    f"Reply draft ready: {draft_url}" if draft_url else "Reply draft could not be created — check the logs."}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"Region {region} · {ref_id}"}]},
            ],
        }
        try:
            _send_to_ops(region, notice_card, log_context=f"UPC {upc} resolved via group")
        except Exception as exc:
            print(f"  ⚠ Could not notify ops of group resolution: {exc!r}", flush=True)

    ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
    return f"{choice}:{key}:{'ok' if ok else 'failed'}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "resend":
        print(json.dumps(resend_unanswered_notices(), ensure_ascii=False, indent=2))
    else:
        demo = {
            "upc": "5064079652602", "artist": "Israel Novaes, MusicEve, Maestro Pinocchio",
            "title": "Comigo é Assim / Lapada Lapada", "label": "Music Eve",
            "spotify_uri": "spotify:track:6cBNGZZVoF1OTaKbYzDyDJ",
            "ref_id": "ref:_00D0992XChO._500Qvj4AvB:ref", "claim_number": "25238364",
            "date_received": "2026-09-26",
        }
        print(json.dumps(build_rights_confirmation_dm_card(demo, "BR"), ensure_ascii=False, indent=2))
