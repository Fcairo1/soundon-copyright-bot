#!/usr/bin/env python3
"""
copyright_alert/metadata_notice.py

Handler for Spotify "Metadata / Misrepresentation" notices.

Spotify sends two DISTINCT email types to the SoundOn copyright inbox:

  1. Infringement claim  — a third-party rights holder claims their content was
     copied. Handled by the existing flow (run_alert / daily_workflow): group
     card + tracker sheet row + Ops DM action card.

  2. Metadata / Misrepresentation Notice (THIS module) — Spotify itself flags a
     release that "may misrepresent a track as originating from or featuring an
     artist, or may use an artist's name in a misleading way". There is NO
     third-party claimant: Spotify is the party raising the notice.

Metadata notices are handled COMPLETELY differently from infringement claims:

  * ❌ NO group card
  * ✅ Metadata Corrections tracker row (BR / SPLA / US)
  * ✅ Parse key fields: UPC, artist, title, label, Spotify URI, ref id
  * ✅ Determine region from the UPC via Aeolus (same lookup as the normal flow)
  * ✅ Send a PRIVATE DM card to the regional Ops owner
        BR   → filipe.cairo
        SPLA → bernardo.sanchez
        US   → ben.gordon-pound (DM chat_id oc_842a762dacdea52dd8cd4017da3a94d5)
  * ✅ Re-send the DM daily until the Ops owner clicks "✅ Actioned"
  * ✅ All state lives in runtime/metadata_notices_state.json (never a sheet)

Applies to ALL regions (BR, SPLA, US).
"""
from __future__ import annotations

import csv
import io
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone

from copyright_alert.run_alert import (
    RUNTIME_DIR,
    _canonical_alert_region,
    _clean_email_value,
    _normalized_body,
    _ops_context_for_region,
    _parse_people_list,
    batch_query_aeolus_by_upc,
    extract_fields,
    first,
    labeled_value,
    patch_card_message,
    query_aeolus,
)
from copyright_alert.state_io import update_json_state

# ── Constants ────────────────────────────────────────────────────────────────
# State file lives alongside the other runtime state (never a tracker sheet).
STATE_FILE = str(RUNTIME_DIR / "metadata_notices_state.json")

# Brazil time (BRT, UTC-3) — the project-wide convention for dates / SLAs.
BRT = timezone(timedelta(hours=-3))

# Callback action string for the single "✅ Actioned" button on the DM card.
CALLBACK_ACTION = "metadata_notice_actioned"

# Admin album link (identical host/params to the infringement group card).
_ADMIN_ALBUM_URL = (
    "https://sg-musician-admin.bytedance.net/avenue/content/album/new"
    "?currentPage=1&pageSize=10&showFields=upc&upc={upc}"
)

METADATA_CORRECTIONS_SHEET_NAME = "Metadata Corrections"
METADATA_CORRECTIONS_BASE_HEADERS = [
    "UPC",
    "Date Received",
    "Spotify Notice Type",
    "Subject",
    "Status",
    "Notes",
]
# Artist 1-4 hold one name each; a 5th+ artist gets comma-joined into Artist 5.
METADATA_ARTIST_HEADERS = ["Artist 1", "Artist 2", "Artist 3", "Artist 4", "Artist 5"]
METADATA_CORRECTIONS_HEADERS = METADATA_CORRECTIONS_BASE_HEADERS + METADATA_ARTIST_HEADERS

# Default status for new rows — matches the infringement-claims tracker's real
# status vocabulary (confirmed in run_alert.py / persistent_callback.py), so a
# metadata-notice row reads consistently next to a claims row for the same UPC.
DEFAULT_METADATA_STATUS = "🔍 Investigating"
_METADATA_TRACKERS = {
    "BR": "https://bytedance.sg.larkoffice.com/sheets/HMQLsGgymhdIQ3tSbNNlk3m1gKd",
    "SPLA": "https://bytedance.larkoffice.com/wiki/Ig1XwJc85iWmsGkEzujcy7sln9d?sheet=66eefc",
    "US": "https://bytedance.sg.larkoffice.com/sheets/FKqxsTu0bhl3ATt3n7YlIGvfgne",
}

# Maximum characters of the raw notice body embedded in the collapsible panel
# (keeps the card well under Lark's size limits).
_MAX_NOTICE_BODY_CHARS = 2500


# ── Time helpers ─────────────────────────────────────────────────────────────
def _today_brt() -> str:
    """Return today's date (YYYY-MM-DD) in Brazil time."""
    return datetime.now(BRT).strftime("%Y-%m-%d")


def _now_brt_iso() -> str:
    return datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S %Z")


# ── Detection ────────────────────────────────────────────────────────────────
def is_metadata_notice(body, subject="", meta=None) -> bool:
    """Return True when an email is a Spotify Metadata / Misrepresentation notice.

    Signals required (ALL must hold):
      * Misrepresentation phrasing — "may misrepresent" / "misrepresent" /
        "misleading way" (or the "Spotify Content Protection" self-identifier).
      * Sender / notifier is Spotify itself (From header on spotify.com, or the
        "Spotify Content Protection" signature).
      * "5 business days" takedown-warning language.
      * NO third-party claimant / rights holder (this is what separates it from a
        DMCA-style infringement claim, which ALSO comes from Spotify but names a
        rights holder / claimant and uses DMCA language).
    """
    meta = meta or {}
    text = f"{_normalized_body(body or '')} {subject or ''}".lower()

    has_misrepresent = ("misrepresent" in text) or ("misleading way" in text) or ("misleading manner" in text)
    has_scp = "spotify content protection" in text

    # Positive phrasing signal (task: "may misrepresent" OR "misleading way" OR
    # "Spotify Content Protection").
    phrase_signal = has_misrepresent or has_scp

    # "5 business days" takedown-warning language.
    five_business_days = bool(re.search(r"\b(?:\d+|(?:five|ten|seven))\s*(?:\(\d+\)\s*)?business\s+days?\b", text))

    # Sender is Spotify themselves.
    head_from = meta.get("head_from") if isinstance(meta.get("head_from"), dict) else {}
    from_addr = str(
        (head_from or {}).get("mail_address", "")
        or meta.get("from", "")
        or meta.get("sender", "")
    ).lower()
    spotify_sender = ("spotify.com" in from_addr) or ("spotify" in from_addr) or has_scp

    # Third-party claimant / rights-holder markers => this is an INFRINGEMENT
    # claim, NOT a metadata notice. Guard against confusing the two.
    infringement_markers = bool(
        re.search(r"\b(dmca|rights?\s*holder|infringement\s+claim|copyright\s+owner)\b", text)
    )
    labeled_claimant = (
        labeled_value(body or "", "Claimant") != "N/A"
        or labeled_value(body or "", "Claimant Name") != "N/A"
    )
    no_claimant = not (infringement_markers or labeled_claimant)

    # The misrepresentation phrasing is the decisive positive marker. Spotify
    # real infringement takedowns can also carry "Spotify Content Protection" +
    # "N business days", so the SCP self-identifier alone is not enough.
    decisive = has_misrepresent

    return bool(phrase_signal and five_business_days and spotify_sender and no_claimant and decisive)


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_metadata_notice(body, subject="", meta=None) -> dict:
    """Extract the key fields from a metadata notice email.

    Reuses ``extract_fields`` for UPC / title / label / ref id / received date,
    then adds artist and Spotify URI parsing specific to this template.
    """
    meta = meta or {}
    body = body or ""
    ef = extract_fields(body, subject, meta)

    upc = str(ef.get("upc", "") or "").strip()
    title = ef.get("title", "N/A")
    label = ef.get("label_name", "N/A")
    ref_id = ef.get("ref_id", "N/A")
    date_received = ef.get("date_received", "N/A")

    # Artist — Spotify notices label it explicitly; fall back to free text.
    artist = labeled_value(body, "Artist", "Artist Name", "Featured Artist", "Primary Artist")
    if artist == "N/A":
        artist = first(body, r"artist(?:\s*name)?\s*[:\-]\s*([^\n\r]+)")
    artist = _clean_email_value(artist) if artist != "N/A" else "N/A"

    # Spotify URI — accept the explicit label, a spotify: URI, or an open.spotify link.
    spotify_uri = labeled_value(body, "Spotify URI", "URI", "Spotify Link")
    if spotify_uri == "N/A":
        spotify_uri = first(body, r"(spotify:(?:track|album|artist):[A-Za-z0-9]+)")
    if spotify_uri == "N/A":
        spotify_uri = first(body, r"(https?://open\.spotify\.com/\S+)")
    spotify_uri = spotify_uri.strip() if spotify_uri != "N/A" else "N/A"

    notice_body = _normalized_body(body).strip()
    if len(notice_body) > _MAX_NOTICE_BODY_CHARS:
        notice_body = notice_body[:_MAX_NOTICE_BODY_CHARS].rstrip() + " …"

    return {
        "upc": upc or "N/A",
        "artist": artist,
        "title": title,
        "label": label,
        "spotify_uri": spotify_uri,
        "ref_id": ref_id,
        "date_received": date_received,
        "notice_body": notice_body or "N/A",
        "subject": subject or "",
    }


def artist_columns_from_display_artist(raw) -> list:
    """Split an Aeolus ``display_artist`` value into the 5 sheet columns.

    ``display_artist`` arrives as a JSON-array-shaped string (e.g.
    '["Anna Luz","DJ Lano SP"]') — confirmed live against the real Metadata
    Corrections tracker's UPCs. Artists 1-4 get one name each; a 5th name and
    any beyond it are comma-joined into Artist 5.
    """
    names = _parse_people_list(raw)
    cols = ["", "", "", "", ""]
    for i, name in enumerate(names[:4]):
        cols[i] = name
    if len(names) > 4:
        cols[4] = ", ".join(names[4:])
    return cols


def notice_key(fields: dict) -> str:
    """Stable, region-agnostic dedup key for a notice."""
    ref_id = str(fields.get("ref_id", "") or "").strip()
    if ref_id and ref_id != "N/A":
        return ref_id
    upc = str(fields.get("upc", "") or "").strip()
    return f"upc:{upc}" if upc and upc != "N/A" else f"raw:{hash(fields.get('subject', ''))}"


# ── State helpers (runtime/metadata_notices_state.json) ──────────────────────
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


# ── Card builder ─────────────────────────────────────────────────────────────
def build_metadata_notice_card(fields: dict, region: str, *, resolved: bool = False,
                               resolved_by: str = "", resolved_at: str = "") -> dict:
    """Build the private DM interactive card for a Spotify metadata notice.

    Header: ⚠️ Spotify Metadata Notice (yellow / warning — NOT red).
    Key fields: UPC (admin link), Artist, Title, Label, Spotify URI, Received.
    Collapsible "Spotify Notice" section holds the full email body.
    One action button: ✅ Actioned (removed once resolved).
    """
    def v(val):
        return val if val and val != "N/A" else "N/A"

    upc_value = str(fields.get("upc", "N/A") or "N/A")
    upc_display = (
        f"[{upc_value}]({_ADMIN_ALBUM_URL.format(upc=upc_value)})"
        if upc_value != "N/A" else "N/A"
    )
    spotify_uri = v(fields.get("spotify_uri"))
    ref_id = v(fields.get("ref_id"))
    key = notice_key(fields)

    status_line = (
        f"✅ **Actioned**{(' by ' + resolved_by) if resolved_by else ''}"
        if resolved else "⏳ **Action needed** — review & fix metadata, then tap Actioned"
    )

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content":
            f"**{v(fields.get('title'))}**\nArtist(s): {v(fields.get('artist'))}\n{status_line}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content":
            "Spotify flags that this release **may misrepresent an artist or use an "
            "artist's name in a misleading way**. This is a Spotify Content Protection "
            "metadata notice — **not** a third-party infringement claim."}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "**⚠️ Notice Details**"}},
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**UPC**\n{upc_display}"}}
                ]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**Artist**\n{v(fields.get('artist'))}"}}
                ]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**Title**\n{v(fields.get('title'))}"}}
                ]},
            ]
        },
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**Label**\n{v(fields.get('label'))}"}}
                ]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**Spotify URI**\n{spotify_uri}"}}
                ]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"**Received**\n{v(fields.get('date_received'))}"}}
                ]},
            ]
        },
        {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {"tag": "plain_text", "content": "Spotify Notice"},
                "subtitle": {"tag": "plain_text", "content": "Full notice text from Spotify"}
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": v(fields.get("notice_body"))}}
            ]
        },
        {"tag": "hr"},
    ]

    if resolved:
        elements.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content":
                f"✅ Actioned{(' by ' + resolved_by) if resolved_by else ''}"
                f"{(' · ' + resolved_at) if resolved_at else ''} · Region {region} · {ref_id}"}
        ]})
    else:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✅ Actioned"},
                    "type": "primary",
                    "value": {
                        "action": CALLBACK_ACTION,
                        "key": key,
                        "upc": upc_value,
                        "region": region,
                        "ref_id": ref_id,
                    },
                }
            ],
        })
        elements.append({"tag": "note", "elements": [
            {"tag": "plain_text", "content":
                f"Spotify Content Protection · Region {region} · {ref_id} · "
                f"Reminders re-send daily until Actioned"}
        ]})

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "yellow",
            "title": {"tag": "plain_text", "content": "⚠️ Spotify Metadata Notice"}
        },
        "elements": elements,
    }


# ── DM sending ───────────────────────────────────────────────────────────────
def _send_notice_dm(fields: dict, region: str, *, resolved: bool = False,
                    resolved_by: str = "", resolved_at: str = "") -> dict:
    """Send (or re-send) the metadata-notice DM card to the region's Ops owner.

    Routing mirrors ``dm_action_card.send_dm_action_card``: prefer the confirmed
    P2P chat_id, then bot-domain open_id, then a resolvable email address.
    """
    from copyright_alert.bot_runtime import _post_api  # lazy import (avoid cycles)
    from copyright_alert.dm_action_card import resolve_open_id  # lazy import

    ops = _ops_context_for_region(region)
    card = build_metadata_notice_card(
        fields, region, resolved=resolved, resolved_by=resolved_by, resolved_at=resolved_at
    )
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
                print(f"  ✓ Metadata-notice DM sent via {id_type} ({rid}) → {mid} "
                      f"[region {region} · UPC {fields.get('upc')}]", flush=True)
                return {"ok": True, "message_id": mid, "receive_id_type": id_type, "receive_id": rid}
            print(f"  ✗ Metadata-notice DM via {id_type} code={resp.get('code')} msg={resp.get('msg')}", flush=True)
        except Exception as exc:
            print(f"  ✗ Metadata-notice DM via {id_type} failed: {exc!r}", flush=True)

    return {"ok": False, "message_id": "", "receive_id_type": "", "receive_id": ""}


def _run_lark_sheets(args, *, input_text=None):
    cmd = ["lark-cli", "sheets", *args]
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "lark-cli sheets command failed").strip())
    output = (proc.stdout or "").strip()
    return json.loads(output) if output else {}


def _metadata_tracker_url(region: str) -> str:
    return _METADATA_TRACKERS.get(str(region or "").upper(), _METADATA_TRACKERS["BR"])


def _ensure_metadata_corrections_sheet(region: str) -> str:
    tracker_url = _metadata_tracker_url(region)
    info = _run_lark_sheets(["+workbook-info", "--url", tracker_url])
    for sheet in (info.get("data") or {}).get("sheets") or []:
        if sheet.get("sheet_name") == METADATA_CORRECTIONS_SHEET_NAME:
            return sheet.get("sheet_id") or ""

    created = _run_lark_sheets([
        "+sheet-create",
        "--url", tracker_url,
        "--title", METADATA_CORRECTIONS_SHEET_NAME,
        "--row-count", "200",
        "--col-count", str(len(METADATA_CORRECTIONS_HEADERS)),
    ])
    sheet_id = str((created.get("data") or {}).get("sheet_id") or "")
    _run_lark_sheets([
        "+csv-put",
        "--url", tracker_url,
        "--sheet-id", sheet_id,
        "--start-cell", "A1",
        "--csv", ",".join(METADATA_CORRECTIONS_HEADERS) + "\n",
    ])
    return sheet_id


def _append_metadata_correction_row(fields: dict, region: str, artist_columns=None) -> bool:
    tracker_url = _metadata_tracker_url(region)
    sheet_id = _ensure_metadata_corrections_sheet(region)
    if not sheet_id:
        raise RuntimeError(f"Missing {METADATA_CORRECTIONS_SHEET_NAME} sheet id for {region}")

    artist_cols = list(artist_columns) if artist_columns else ["", "", "", "", ""]
    row = [
        fields.get("upc", "N/A"),
        fields.get("date_received", "N/A"),
        "Artist/origin metadata misrepresentation",
        fields.get("subject", ""),
        DEFAULT_METADATA_STATUS,
        f"Auto-routed from Spotify metadata correction notice; ref={fields.get('ref_id', 'N/A')}",
        *artist_cols,
    ]
    payload = {"sheets": [{
        "name": METADATA_CORRECTIONS_SHEET_NAME,
        "mode": "append",
        "header": False,
        "columns": METADATA_CORRECTIONS_HEADERS,
        "data": [row],
        "dtypes": {header: "object" for header in METADATA_CORRECTIONS_HEADERS},
    }]}
    _run_lark_sheets(["+table-put", "--url", tracker_url, "--sheets", "-"], input_text=json.dumps(payload))
    return True


# ── Backfill (Artist 1-5 + blank-status fill on the existing tracker) ────────
def _column_letter(index0: int) -> str:
    """0-indexed column number -> spreadsheet letter (0->A, 6->G, ...)."""
    letters = ""
    n = index0 + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _rows_to_csv(rows) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def _read_metadata_tracker_rows(region: str):
    """Read every row of the region's Metadata Corrections tab.

    Returns (header: list[str], data_rows: list[list[str]], sheet_id: str).
    Blank trailing rows in the sheet's padded range are dropped.
    """
    tracker_url = _metadata_tracker_url(region)
    sheet_id = _ensure_metadata_corrections_sheet(region)
    if not sheet_id:
        raise RuntimeError(f"Missing {METADATA_CORRECTIONS_SHEET_NAME} sheet id for {region}")

    result = _run_lark_sheets([
        "+csv-get", "--url", tracker_url, "--sheet-id", sheet_id,
        "--include-row-prefix=false",
    ])
    csv_text = (result.get("data") or {}).get("annotated_csv") or ""
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return [], [], sheet_id
    header = [h.strip() for h in rows[0]]
    data_rows = [r for r in rows[1:] if any(c.strip() for c in r)]
    return header, data_rows, sheet_id


def backfill_metadata_artists_and_status(region: str = "BR", *, dry_run: bool = True) -> dict:
    """Backfill Artist 1-5 and blank Status cells on an existing tracker tab.

    * Adds the Artist 1-5 headers if the sheet doesn't already have them.
    * Resolves every unique UPC's artist names in ONE batched Aeolus query
      (batch_query_aeolus_by_upc), not one query per row — an earlier feature
      on this same tracker family (marketshare backfill) hit real 600s
      timeouts from exactly that per-row pattern.
    * Overwrites the Artist columns for every row — they're always computed
      from Aeolus, never ops-edited, so re-running this is always safe.
    * Fills Status ONLY where it is currently blank. Every non-blank value
      (including "New", the default written before this change shipped) is
      left exactly as-is — this was an explicit scope decision, not an
      oversight, so most existing rows will keep reading "New".
    """
    tracker_url = _metadata_tracker_url(region)
    header, data_rows, sheet_id = _read_metadata_tracker_rows(region)
    if not header:
        raise RuntimeError(f"Could not read {METADATA_CORRECTIONS_SHEET_NAME} header for {region}")

    def _hidx(name):
        return header.index(name) if name in header else None

    upc_i = _hidx("UPC")
    status_i = _hidx("Status")
    if upc_i is None or status_i is None:
        raise RuntimeError(f"{METADATA_CORRECTIONS_SHEET_NAME} header missing UPC/Status: {header}")

    upcs = [(r[upc_i].strip() if len(r) > upc_i else "") for r in data_rows]
    unique_upcs = sorted({u for u in upcs if u})
    aeolus_rows = batch_query_aeolus_by_upc(unique_upcs) if unique_upcs else {}

    # Counted over unique UPCs (aeolus_rows is keyed by unique UPC) — NOT per
    # data row, which would double-count a UPC that appears in multiple rows
    # and could make upcs_not_found go negative.
    resolved = len(aeolus_rows)
    artist_rows = []
    for upc in upcs:
        row_data = aeolus_rows.get(upc, {})
        artist_rows.append(artist_columns_from_display_artist(row_data.get("display_artist")))

    filled_blank = 0
    new_status = []
    for r in data_rows:
        current = r[status_i].strip() if len(r) > status_i else ""
        if not current:
            new_status.append(DEFAULT_METADATA_STATUS)
            filled_blank += 1
        else:
            new_status.append(current)

    has_artist_headers = all(h in header for h in METADATA_ARTIST_HEADERS)
    artist_start_col = _column_letter(len(METADATA_CORRECTIONS_BASE_HEADERS))  # "G" today
    status_col = _column_letter(status_i)

    summary = {
        "region": region,
        "rows": len(data_rows),
        "unique_upcs": len(unique_upcs),
        "upcs_resolved_via_aeolus": resolved,
        "upcs_not_found": len(unique_upcs) - resolved,
        "headers_already_present": has_artist_headers,
        "status_blank_filled": filled_blank,
        "dry_run": dry_run,
        "sample": [
            {"upc": upcs[i], "artists": artist_rows[i], "status": new_status[i]}
            for i in range(min(3, len(data_rows)))
        ],
    }
    if dry_run:
        return summary

    if not has_artist_headers:
        _run_lark_sheets([
            "+csv-put", "--url", tracker_url, "--sheet-id", sheet_id,
            "--start-cell", f"{artist_start_col}1",
            "--csv", _rows_to_csv([METADATA_ARTIST_HEADERS]),
        ])

    _run_lark_sheets([
        "+csv-put", "--url", tracker_url, "--sheet-id", sheet_id,
        "--start-cell", f"{artist_start_col}2",
        "--csv", _rows_to_csv(artist_rows),
    ])

    if filled_blank:
        _run_lark_sheets([
            "+csv-put", "--url", tracker_url, "--sheet-id", sheet_id,
            "--start-cell", f"{status_col}2",
            "--csv", _rows_to_csv([[v] for v in new_status]),
        ])

    summary["headers_written"] = not has_artist_headers
    summary["artist_rows_written"] = len(artist_rows)
    summary["status_rows_written"] = filled_blank
    return summary


# ── Main handler (called from the daily scan) ────────────────────────────────
def handle_metadata_notice(body, subject="", meta=None, msg_id="") -> dict:
    """Route a detected metadata notice: parse → region → DM → track state.

    Idempotent across regional scans: the first scan that sees a given notice
    (keyed by ref id / UPC) sends the DM and records it; later scans (including
    the other regions' daily runs that see the same inbox email) find it already
    tracked and skip. Daily re-sends are driven by ``resend_unresolved_notices``.
    """
    fields = parse_metadata_notice(body, subject, meta)
    key = notice_key(fields)

    # Region from UPC via Aeolus (same lookup as the infringement flow).
    upc = str(fields.get("upc", "") or "").strip()
    aeolus_row = query_aeolus(upc) if upc and upc != "N/A" else {}
    region = _canonical_alert_region(aeolus_row=aeolus_row) if aeolus_row else "BR"

    # Fill artist / title gaps from Aeolus when the email omitted them.
    if fields.get("artist", "N/A") == "N/A" and aeolus_row.get("display_artist"):
        fields["artist"] = str(aeolus_row.get("display_artist"))
    if fields.get("title", "N/A") == "N/A" and aeolus_row.get("album_title"):
        fields["title"] = str(aeolus_row.get("album_title"))

    artist_columns = artist_columns_from_display_artist(aeolus_row.get("display_artist"))

    existing = get_notice(key)
    if existing:
        # Already tracked — record that we saw it again, but do not re-send here
        # (the daily loop owns re-sends). Resolved notices stay resolved.
        def _touch(state):
            rec = state["notices"].get(key)
            if rec is not None:
                rec["last_seen"] = _now_brt_iso()
        update_json_state(STATE_FILE, _touch, default=lambda: {"notices": {}})
        print(f"  • Metadata notice already tracked ({key}, region {region}, "
              f"resolved={existing.get('resolved')}) — skipping duplicate", flush=True)
        return {"status": "already_tracked", "key": key, "region": region}

    # New notice → write to the Metadata Corrections tab, send the DM, and record state.
    tracker_ok = _append_metadata_correction_row(fields, region, artist_columns)
    send = _send_notice_dm(fields, region)
    today = _today_brt()

    def _insert(state):
        state["notices"][key] = {
            "key": key,
            "region": region,
            "fields": fields,
            "source_email_message_id": msg_id or "",
            "first_seen": _now_brt_iso(),
            "last_seen": _now_brt_iso(),
            "last_dm_sent": today if send.get("ok") else "",
            "dm_count": 1 if send.get("ok") else 0,
            "message_id": send.get("message_id", ""),
            "receive_id_type": send.get("receive_id_type", ""),
            "receive_id": send.get("receive_id", ""),
            "tracker_row_written": tracker_ok,
            "resolved": False,
            "resolved_at": "",
            "resolved_by": "",
        }
    update_json_state(STATE_FILE, _insert, default=lambda: {"notices": {}})

    print(f"  ✓ Metadata notice recorded ({key}, region {region}, tracker ok={tracker_ok}, DM ok={send.get('ok')})", flush=True)
    return {"status": "new", "key": key, "region": region, "tracker_ok": tracker_ok, "dm_ok": send.get("ok")}


# ── Daily re-send loop ───────────────────────────────────────────────────────
def resend_unresolved_notices() -> dict:
    """Re-send the DM for every unresolved notice not yet re-sent today (BRT).

    The per-day guard (``last_dm_sent < today``) makes this safe to call from
    each region's daily workflow: only the first run of the day re-sends a given
    notice; subsequent same-day runs skip it.
    """
    state = _load_state()
    notices = state.get("notices") or {}
    today = _today_brt()
    summary = {"total": len(notices), "resent": 0, "skipped_today": 0,
               "skipped_resolved": 0, "failed": 0}

    for key, rec in notices.items():
        if rec.get("resolved"):
            summary["skipped_resolved"] += 1
            continue
        if rec.get("last_dm_sent") == today:
            summary["skipped_today"] += 1
            continue

        region = rec.get("region", "BR")
        fields = rec.get("fields", {})
        send = _send_notice_dm(fields, region)

        def _update(st, _key=key, _send=send):
            r = st["notices"].get(_key)
            if r is None:
                return
            if _send.get("ok"):
                r["last_dm_sent"] = today
                r["dm_count"] = int(r.get("dm_count", 0)) + 1
                r["message_id"] = _send.get("message_id", r.get("message_id", ""))
                r["receive_id_type"] = _send.get("receive_id_type", r.get("receive_id_type", ""))
                r["receive_id"] = _send.get("receive_id", r.get("receive_id", ""))
        update_json_state(STATE_FILE, _update, default=lambda: {"notices": {}})

        if send.get("ok"):
            summary["resent"] += 1
        else:
            summary["failed"] += 1

    print(f"  Metadata-notice re-send: {summary}", flush=True)
    return summary


# ── "✅ Actioned" button handler (called from the callback daemon) ────────────
def mark_actioned(key: str, *, resolved_by: str = "") -> dict:
    """Mark a notice resolved so daily re-sends stop. Returns the notice record."""
    resolved_at = _now_brt_iso()

    def _resolve(state):
        rec = state["notices"].get(key)
        if rec is not None:
            rec["resolved"] = True
            rec["resolved_at"] = resolved_at
            rec["resolved_by"] = resolved_by or ""
    update_json_state(STATE_FILE, _resolve, default=lambda: {"notices": {}})
    return get_notice(key)


def handle_actioned_callback(value: dict, message_id: str = "", operator: str = "") -> str:
    """Resolve a notice from an "✅ Actioned" button click and refresh the card.

    Returns a short status string for logging / the toast.
    """
    key = str((value or {}).get("key", "") or "").strip()
    region = str((value or {}).get("region", "") or "BR").strip() or "BR"
    if not key:
        return "no key"

    rec = mark_actioned(key, resolved_by=operator)
    fields = (rec or {}).get("fields", {})

    # Re-render the DM card as resolved (button removed) so the click is visible.
    if message_id and fields:
        try:
            card = build_metadata_notice_card(
                fields, rec.get("region", region), resolved=True,
                resolved_by=rec.get("resolved_by", operator),
                resolved_at=rec.get("resolved_at", ""),
            )
            patch_card_message(message_id, card)
        except Exception as exc:
            print(f"  ⚠ Could not patch metadata-notice card {message_id}: {exc!r}", flush=True)

    return f"actioned:{key}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "resend":
        print(json.dumps(resend_unresolved_notices(), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        _args = sys.argv[2:]
        _dry_run = "--dry-run" in _args
        _region_arg = next((a.split("=", 1)[1] for a in _args if a.startswith("--region=")), "ALL").upper()
        _regions = list(_METADATA_TRACKERS) if _region_arg == "ALL" else [_region_arg]
        _results = {r: backfill_metadata_artists_and_status(r, dry_run=_dry_run) for r in _regions}
        print(json.dumps(_results, ensure_ascii=False, indent=2))
    else:
        # Demo: build a sample card.
        demo = {
            "upc": "0197342112345", "artist": "Fake Artist", "title": "Misleading Track",
            "label": "Some Label", "spotify_uri": "spotify:album:1a2b3c4d5e",
            "ref_id": "ref:_00Dxxref", "date_received": "2026-07-23",
            "notice_body": "Your release may misrepresent an artist ... within 5 business days.",
            "subject": "Spotify Content Protection — Metadata Notice",
        }
        print(json.dumps(build_metadata_notice_card(demo, "BR"), ensure_ascii=False, indent=2))
