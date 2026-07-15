#!/usr/bin/env python3
"""
copyright_alert/daily_workflow.py

Master daily workflow for the automated copyright infringement alert system.
Runs the following sections in sequence (see PART 1 of the spec):

  A) Incremental inbox scan + post cards
       - Checkpoint-aware: only processes emails newer than the last run.
       - Filters BR + (source AP/A&R OR tier High Quality) via Aeolus.
       - Skips duplicates already in posted_claims.json.
       - Posts a Lark card per qualifying case to the alert group.
  D) Ensure the tracker sheet has an "Admin Action Taken" column (last header).
       - Run before B/C so the empty-admin-action checks are accurate.
  B) Remind filipe.cairo about rows whose Status is empty/unset.
  C) Action alert to filipe.cairo:
       - Status "🔴 Confirm Takedown" + Admin Action Taken empty -> needs takedown on admin
       - Status "✅ Resolved"        + Admin Action Taken empty -> needs "Assert" on admin

This script intentionally REUSES the proven helpers in run_alert.py so the card
format, Aeolus lookup, dedup and tracker-append logic stay identical to the
single-shot scanner.

It does NOT stop after the first qualifying post (unlike run_alert.main); it
processes every new qualifying email within the checkpoint window.

All output is written to both stdout and copyright_alert/logs/daily_<ts>.log.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from copyright_alert.lark_auth import extract_sheet_values, request_json_with_auth_retry, sheet_values_api

# ── Make the copyright_alert package importable & anchor relative paths ───────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # run_alert helpers use relative paths like "copyright_alert/..."

from copyright_alert.run_alert import (  # noqa: E402
    MAILBOX,
    TARGET_CHAT_ID,
    TRACKER_SHEET_URL,
    TRACKER_SHEET_ID,
    TRIAGE_QUERY,
    parse_lark_json,
    fetch_email,
    extract_fields,
    batch_query_aeolus_by_upc,
    qualifies,
    claim_key,
    is_claim_already_posted,
    _save_posted_claim,
    _atomic_write_json,
    _load_posted_claims,
    POSTED_CLAIMS_FILE,
    save_posted_card,
    build_card,
    post_card,
    patch_card_message,
    append_tracker_row,
    _format_artist_names,
    _get_bot_access_token,
    load_posted_card,
)
from copyright_alert.dm_action_card import (  # noqa: E402
    send_dm_action_card,
    parse_detected_date,
)

# ── Config ───────────────────────────────────────────────────────────────────
CHECKPOINT_FILE = "copyright_alert/scan_checkpoint.json"
LAST_CARD_FILE = "copyright_alert/last_card.json"
LOG_DIR = "copyright_alert/logs"
TRIAGE_MAX = 50
# G2: A failed message is retried at most this many times before it is dropped
# from the retry list, so a message that will never succeed is not re-processed
# on every run forever.
MAX_RETRY_ATTEMPTS = 5
ACTIVE_REGION = "BR"  # region this workflow run is configured for
RECIPIENT_EMAIL = "filipe.cairo@bytedance.com"  # filipe.cairo — personal alert DM target (BR default)
RECIPIENT_OPEN_ID = ""  # when set, ops DMs go to this open_id via the copyright bot
RECIPIENT_CHAT_ID = ""  # optional confirmed DM chat_id for the ops owner
FEISHU_IM_DIR = ROOT / "inner_skills" / "feishu-im-send"
ADMIN_ACTION_HEADER = "Admin Action Taken"
STATUS_TAKEDOWN = "🔴 Confirm Takedown"
STATUS_RESOLVED = "✅ Resolved"

# Spotify reply workflow (PART 1E / 1F)
CARD_MSG_ID_HEADER = "Card Message ID"
LARK_MSG_ID_HEADER = "Lark Message ID"
EMAIL_STATUS_HEADER = "Email Status"
DATE_RECEIVED_HEADER = "Date Received"
UPC_HEADER = "UPC"
ISRC_HEADER = "ISRC"
TITLE_HEADER = "Title"
ARTIST_HEADER = "Artist(s)"
CLAIMANT_HEADER = "Claimant"
STATUS_HEADER = "Status"
DSP_HEADER = "DSP"
# New tracker column (appended after the last existing column) that stores the
# Spotify claim reference code (e.g. "ref:_00D0992XChO._500QvfHqBc:ref").
SPOTIFY_REF_HEADER = "Spotify Ref Code"
POSTED_CLAIMS_FILES = [
    "copyright_alert/posted_claims.json",
    "copyright_alert/posted_claims_ap_direitos_br.json",
]
SPOTIFY_DM_STATE_FILE = "copyright_alert/spotify_dm_sent.json"
# C1: the reply deadline is unified to 5 BUSINESS days in BRT. The single source
# of truth lives in tag_managers; do not reintroduce a local calendar-day value.
from copyright_alert.tag_managers import business_days_remaining_brt, REPLY_DEADLINE_WORKDAYS  # noqa: E402


# ── Region configuration ─────────────────────────────────────────────────────
def configure_region(region):
    """Point this workflow at one region's group/tracker/ops owner.

    Reconfigures both run_alert (used by post_card / append_tracker_row /
    qualifies) and this module's own globals (used by the sheet + DM helpers).
    Each region keeps an isolated scan checkpoint so concurrent regional runs do
    not skip each other's inbox messages.
    """
    global ACTIVE_REGION, TARGET_CHAT_ID, TRACKER_SHEET_URL, TRACKER_SHEET_ID
    global RECIPIENT_EMAIL, RECIPIENT_OPEN_ID, RECIPIENT_CHAT_ID, CHECKPOINT_FILE

    from copyright_alert import bot_runtime as br

    region = (region or "BR").upper()
    cfg = br.configure_region(region)  # sets run_alert globals + qualify countries

    ACTIVE_REGION = region
    TARGET_CHAT_ID = cfg["chat_id"]
    TRACKER_SHEET_URL = cfg["tracker_url"]
    TRACKER_SHEET_ID = cfg["sheet_id"]
    RECIPIENT_EMAIL = cfg.get("ops_dm_email") or RECIPIENT_EMAIL
    RECIPIENT_OPEN_ID = cfg.get("ops_dm_open_id") or ""
    RECIPIENT_CHAT_ID = cfg.get("ops_dm_chat_id") or ""
    # BR keeps the original checkpoint path for backward compatibility.
    CHECKPOINT_FILE = (
        "copyright_alert/scan_checkpoint.json"
        if region == "BR"
        else f"copyright_alert/scan_checkpoint_{region}.json"
    )
    return cfg


# ── Logging (tee to file + stdout) ───────────────────────────────────────────
class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"daily_{ts}.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_fh)
    sys.stderr = _Tee(sys.__stderr__, log_fh)
    return log_path


def log(msg=""):
    print(msg, flush=True)


def section(title):
    log("\n" + "=" * 72)
    log(title)
    log("=" * 72)


# ── Checkpoint ───────────────────────────────────────────────────────────────
def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE):
        return None
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return (data or {}).get("last_message_id")
    except Exception as e:
        log(f"  ⚠ Could not read checkpoint: {e!r}")
        return None


def load_failed_message_ids():
    """Return the persisted retry list of previously-failed messages.

    Each entry is a dict with at least ``message_id`` plus ``subject``/``date``
    hints so the next run can re-process it before touching new mail (B4).
    """
    if not os.path.exists(CHECKPOINT_FILE):
        return []
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        failed = (data or {}).get("failed_message_ids") or []
        normalized = []
        for item in failed:
            if isinstance(item, dict) and item.get("message_id"):
                normalized.append({
                    "message_id": item.get("message_id"),
                    "subject": item.get("subject", ""),
                    "date": item.get("date", ""),
                    # G2: retry attempt counter. Backward-compatible: entries
                    # written before G2 lack this key and default to 1.
                    "attempts": int(item.get("attempts") or 1),
                })
            elif isinstance(item, str) and item:
                normalized.append({"message_id": item, "subject": "", "date": "", "attempts": 1})
        return normalized
    except Exception as e:
        log(f"  ⚠ Could not read failed_message_ids from checkpoint: {e!r}")
        return []


def save_checkpoint(message_id, failed_message_ids=None):
    """Persist the incremental high-water mark and the failed-retry list.

    ``failed_message_ids`` must be the *current* full retry list (a list of
    dicts). It is always written so that messages which succeeded on retry are
    dropped and newly-failed messages are added. Passing ``None`` preserves the
    previously stored list (used by callers that only advance the high-water
    mark and do not track failures).
    """
    if not message_id and failed_message_ids is None:
        return
    if failed_message_ids is None:
        failed_message_ids = load_failed_message_ids()
    payload = {
        "last_message_id": message_id,
        "failed_message_ids": failed_message_ids or [],
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    os.makedirs(os.path.dirname(CHECKPOINT_FILE), exist_ok=True)
    _atomic_write_json(CHECKPOINT_FILE, payload, ensure_ascii=False, indent=2)
    log(f"  ✓ Checkpoint saved: {message_id} (retry list: {len(failed_message_ids or [])})")


# ── Mail fetching ────────────────────────────────────────────────────────────
def fetch_messages_raw():
    """Fetch up to TRIAGE_MAX inbox messages, newest-first, as raw dicts."""
    cmd = [
        "lark-cli", "mail", "+triage",
        "--mailbox", MAILBOX,
        "--query", TRIAGE_QUERY,
        "--max", str(TRIAGE_MAX),
        "--format", "json",
    ]
    log(f"Fetching inbox: query={TRIAGE_QUERY!r}, max={TRIAGE_MAX}, mailbox={MAILBOX}")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        log(f"  ✗ triage failed rc={res.returncode}: {(res.stdout + res.stderr)[:500]}")
        return []
    parsed = parse_lark_json(res.stdout)
    if not parsed:
        log(f"  ✗ Failed to parse triage output. Raw (first 400): {res.stdout[:400]}")
        return []
    messages = parsed.get("messages") or (parsed.get("data") or {}).get("messages") or []
    log(f"  Fetched {len(messages)} emails (newest-first).")
    return messages


def _prefilter_skip_reason(subject, thread_id, seen_threads):
    if re.match(r"(?i)^(re:|fw:)", subject or ""):
        return "reply/forward subject"
    if "claim release" in (subject or "").lower():
        return "claim release subject"
    if thread_id and thread_id in seen_threads:
        return f"duplicate thread {thread_id}"
    return None


def _parse_candidate(msg_id, subject, date, thread_id, seen_threads, summary):
    """Parse a single inbox message into a scan candidate.

    Returns a tuple ``(kind, info)`` where ``kind`` is one of:
      - ``"candidate"``: ``info`` is the candidate dict ready for Aeolus/posting.
      - ``"skip"``: intentionally skipped (pre-filter / no identifier). ``info``
        is a short reason string. The checkpoint may safely advance past it.
      - ``"failed"``: transient failure (e.g. could not fetch body). ``info`` is
        a reason string. The caller must add it to the retry list (B4).
    """
    reason = _prefilter_skip_reason(subject, thread_id, seen_threads)
    if reason:
        log(f"  • SKIP ({reason}): {subject[:60]}")
        summary["skipped_prefilter"] += 1
        return ("skip", reason)
    seen_threads.add(thread_id)

    summary["examined"] += 1
    log(f"\n  ── Parsing candidate: {date} | {subject[:70]}")
    log(f"     message_id: {msg_id}")

    body, meta = fetch_email(msg_id)
    if not body:
        log("     ✗ Could not fetch body — will retry next run")
        return ("failed", "fetch body failed")

    ef = extract_fields(body, subject, meta)
    upc = str(ef.get("upc", "") or "").strip()
    isrc = ef.get("isrc", "")
    log(f"     UPC={upc or 'N/A'} ISRC={isrc}")

    if not upc or upc == "N/A":
        log("     ✗ No UPC, skipping")
        summary["skipped_no_identifier"] += 1
        return ("skip", "no identifier")

    return ("candidate", {
        "message_id": msg_id,
        "subject": subject,
        "date": date,
        "ef": ef,
    })


# ── PART 1A — incremental scan + post ────────────────────────────────────────
def run_scan():
    section("PART 1A — Incremental inbox scan + post cards")
    messages = fetch_messages_raw()
    summary = {
        "fetched": len(messages),
        "examined": 0,
        "parsed_candidates": 0,
        "unique_upcs": 0,
        "posted": 0,
        "skipped_duplicate": 0,
        "skipped_not_qualifying": 0,
        "skipped_no_aeolus": 0,
        "skipped_no_identifier": 0,
        "skipped_prefilter": 0,
        "retried_previous_failures": 0,
        "failed_pending_retry": 0,
        "stopped_at_checkpoint": False,
    }

    checkpoint = load_checkpoint()
    prev_failed = load_failed_message_ids()
    # G2: map message_id → prior attempt count so we can increment per run and
    # drop entries that have exhausted their retry budget.
    prev_attempts = {
        item["message_id"]: int(item.get("attempts") or 1)
        for item in prev_failed
        if item.get("message_id")
    }

    if not messages and not prev_failed:
        log("  ⚠ No emails fetched and no pending retries; nothing to scan.")
        return summary

    # new_checkpoint advances the high-water mark to the newest fetched message.
    # Messages that fail this run are NOT lost: they are persisted to the
    # failed_message_ids retry list and re-processed at the start of next run.
    new_checkpoint = (messages[0].get("message_id") if messages else None) or checkpoint
    log(f"  Previous checkpoint: {checkpoint or '(none — first run, will process all fetched)'}")

    seen_threads = set()
    candidates = []
    # message_id -> {message_id, subject, date} for messages that must be retried.
    failed_entries = {}

    # ── Phase 0 — retry previously-failed messages BEFORE new mail (B4) ──────
    if prev_failed:
        summary["retried_previous_failures"] = len(prev_failed)
        log(f"  ↻ Retrying {len(prev_failed)} previously-failed message(s) before new mail …")
    for item in prev_failed:
        rid = item.get("message_id")
        if not rid:
            continue
        r_subject = item.get("subject", "")
        r_date = item.get("date", "")
        kind, info = _parse_candidate(rid, r_subject, r_date, rid, seen_threads, summary)
        if kind == "candidate":
            candidates.append(info)
        elif kind == "failed":
            failed_entries[rid] = {"message_id": rid, "subject": r_subject, "date": r_date}
        # "skip" ⇒ intentionally resolved; drop from the retry list.

    # ── Phase 1 — scan new messages up to the checkpoint ─────────────────────
    for m in messages:
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        date = m.get("date", "")
        thread_id = m.get("thread_id") or msg_id

        if checkpoint and msg_id == checkpoint:
            log(f"  ⏹ Reached checkpoint at {msg_id} — stopping scan (older emails already processed).")
            summary["stopped_at_checkpoint"] = True
            break

        if not msg_id:
            continue

        kind, info = _parse_candidate(msg_id, subject, date, thread_id, seen_threads, summary)
        if kind == "candidate":
            candidates.append(info)
        elif kind == "failed":
            failed_entries[msg_id] = {"message_id": msg_id, "subject": subject, "date": date}

    summary["parsed_candidates"] = len(candidates)
    aeolus_by_upc = batch_query_aeolus_by_upc([c["ef"].get("upc") for c in candidates])
    summary["unique_upcs"] = len(aeolus_by_upc)
    summary["engagement_upcs"] = 0

    for c in candidates:
        msg_id = c["message_id"]
        subject = c["subject"]
        ef = c["ef"]
        upc = str(ef.get("upc", "") or "").strip()
        ar = aeolus_by_upc.get(upc) or {}

        if not ar:
            log(f"     ✗ No Aeolus data for UPC {upc} — will retry next run")
            summary["skipped_no_aeolus"] += 1
            failed_entries[msg_id] = {"message_id": msg_id, "subject": subject, "date": c.get("date", "")}
            continue

        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"

        if not qualifies(ar):
            summary["skipped_not_qualifying"] += 1
            failed_entries.pop(msg_id, None)
            continue

        dup_key = claim_key(ef, ar, subject)
        if is_claim_already_posted(dup_key):
            log(f"     ✗ Duplicate already posted: {dup_key}")
            summary["skipped_duplicate"] += 1
            failed_entries.pop(msg_id, None)
            continue

        log(f"     ✓ Qualifies & new — posting card for UPC {upc} …")
        card = build_card(ef, ar, region=ACTIVE_REGION)
        with open(LAST_CARD_FILE, "w", encoding="utf-8") as fh:
            json.dump(card, fh, indent=2)

        success, posted_message_id = post_card(card, ar, upc=upc, context=f"{ACTIVE_REGION} daily scan group post")
        if success and posted_message_id:
            card = build_card(
                ef,
                ar,
                lark_message_id=posted_message_id,
                source_email_message_id=msg_id,
                region=ACTIVE_REGION,
                ops_dm_email=RECIPIENT_EMAIL,
                ops_dm_open_id=RECIPIENT_OPEN_ID,
                ops_dm_chat_id=RECIPIENT_CHAT_ID,
            )
            with open(LAST_CARD_FILE, "w", encoding="utf-8") as fh:
                json.dump(card, fh, indent=2)
            # Register the freshly-posted card's state to disk (posted_cards.json)
            # at creation time, BEFORE the follow-up PATCH. Button clicks load the
            # card via load_posted_card(); persisting here guarantees a saved copy
            # exists even if patch_card_message() below fails or the daemon
            # restarts, so the first click never hits "no saved copy exists".
            save_posted_card(posted_message_id, card)
            patch_card_message(posted_message_id, card)
            tracker_row = append_tracker_row(ef, ar, posted_message_id, status="")
            _save_posted_claim(dup_key, {
                "message_id": posted_message_id,
                "source_email_message_id": msg_id,
                "subject": subject,
                "upc": ef.get("upc", "N/A"),
                "isrc": ef.get("isrc", "N/A"),
                "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
                "artist": _format_artist_names(ar.get("display_artist")),
                "ref_id": ef.get("ref_id", "N/A"),
                "claimant_name": ef.get("claimant_name", "N/A"),
                "claimant_email": ef.get("claimant_email", "N/A"),
                "region": ACTIVE_REGION,
                "tracker_row": tracker_row,
                "chat_id": TARGET_CHAT_ID,
            })
            summary["posted"] += 1
            failed_entries.pop(msg_id, None)
            log(f"     ✅ Posted card {posted_message_id} for UPC {upc}")
        else:
            log("     ✗ Card posting failed for this candidate — will retry next run")
            failed_entries[msg_id] = {"message_id": msg_id, "subject": subject, "date": c.get("date", "")}

    if checkpoint and not summary["stopped_at_checkpoint"] and len(messages) >= TRIAGE_MAX:
        log(f"  🚨 WARNING: previous checkpoint {checkpoint} was NOT reached within the "
            f"{TRIAGE_MAX}-message fetch window. More than {TRIAGE_MAX} new messages may have "
            f"arrived since the last run; messages older than this window will be permanently "
            f"skipped once the checkpoint advances. Consider raising TRIAGE_MAX or paginating.")

    # G2: Attach/increment attempt counters and drop entries that have exhausted
    # their retry budget so a permanently-failing message is not retried forever.
    failed_list = []
    dropped = []
    for entry in failed_entries.values():
        mid = entry.get("message_id")
        attempts = prev_attempts.get(mid, 0) + 1
        entry["attempts"] = attempts
        if attempts > MAX_RETRY_ATTEMPTS:
            log(f"  ⚠ Dropping message {mid} from retry list after {attempts - 1} failed attempts (giving up).")
            dropped.append(entry)
            continue
        failed_list.append(entry)
    summary["failed_pending_retry"] = len(failed_list)
    summary["dropped_after_max_retries"] = len(dropped)
    if dropped:
        # Best-effort notify the ops owner so a stuck message is not silently lost.
        try:
            send_dm_post(
                f"⚠️ {len(dropped)} message(s) dropped from the retry list",
                [f"• {d.get('message_id')} — {d.get('subject') or 'N/A'} (after {MAX_RETRY_ATTEMPTS} failed attempts)"
                 for d in dropped],
            )
        except Exception as exc:
            log(f"  ⚠ Could not DM ops about dropped retries: {exc!r}")
    save_checkpoint(new_checkpoint, failed_message_ids=failed_list)
    log(f"\n  Scan summary: {json.dumps(summary, ensure_ascii=False)}")
    return summary


# ── Sheet helpers ────────────────────────────────────────────────────────────
def _col_letter(index):
    """Zero-based column index -> spreadsheet column letters."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _norm(v):
    return str(v if v is not None else "").strip()


def _normalized_status(v):
    """Normalize tracker/card status for comparisons.

    The sheet may contain either the full button label ("✅ Resolved") or a
    manually typed value ("Resolved", different case, extra spaces). Treat both
    as the same state so resolved rows are never considered open follow-ups.
    """
    status = _norm(v).casefold()
    status = re.sub(r"^[^\w]+\s*", "", status).strip()
    return status


def _is_resolved_status(v):
    return _normalized_status(v) == "resolved"


def _is_confirm_takedown_status(v):
    return _normalized_status(v) == "confirm takedown"


def _admin_action_has_real_value(v):
    normalized = _norm(v).casefold()
    return bool(normalized) and normalized != "no"


def _is_open_for_ops(status, admin_action=""):
    normalized = _normalized_status(status)
    if normalized == "resolved":
        return False
    if normalized == "confirm takedown":
        return not _admin_action_has_real_value(admin_action)
    return normalized in {"", "investigating", "disputing", "pending", "open"}


def read_sheet_values(rng="A:Z"):
    """Read tracker rows as a 2D list using persisted Lark OAuth, with legacy CLI fallback."""
    try:
        return extract_sheet_values(sheet_values_api("GET", TRACKER_SHEET_URL, TRACKER_SHEET_ID, rng))
    except Exception as exc:
        log(f"  ⚠ Sheet read via OAuth failed; trying legacy lark-cli fallback: {exc!r}")
    cmd = [
        "lark-cli", "sheets", "+read", "--url", TRACKER_SHEET_URL,
        "--sheet-id", TRACKER_SHEET_ID, "--range", rng, "--format", "json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        log(f"  ✗ Sheet read failed: {(res.stdout + res.stderr)[:400]}")
        return []
    parsed = parse_lark_json(res.stdout)
    if not parsed:
        return []
    return extract_sheet_values(parsed)


def write_cell(col_letter, row_num, value):
    # Harden: ensure value is written as a string to the sheet
    val_str = str(value if value is not None else "")
    cell_range = f"{col_letter}{row_num}:{col_letter}{row_num}"
    try:
        sheet_values_api("PUT", TRACKER_SHEET_URL, TRACKER_SHEET_ID, cell_range, values=[[val_str]])
        log(f"  Sheet write {col_letter}{row_num} via OAuth -> {value}")
        return True
    except Exception as exc:
        log(f"  ⚠ Sheet write {col_letter}{row_num} via OAuth failed; trying legacy lark-cli fallback: {exc!r}")
    cmd = [
        "lark-cli", "sheets", "+write", "--url", TRACKER_SHEET_URL,
        "--sheet-id", TRACKER_SHEET_ID, "--range", cell_range,
        "--values", json.dumps([[val_str]], ensure_ascii=False),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    ok = res.returncode == 0
    log(f"  Sheet write {col_letter}{row_num} rc={res.returncode} -> {value}")
    if not ok:
        log(f"    {(res.stdout + res.stderr)[:300]}")
    return ok


# ── PART 1D — ensure Admin Action Taken column ───────────────────────────────
def ensure_admin_action_column(values):
    """Ensure the tracker has an 'Admin Action Taken' header as the last column.

    Returns (admin_col_index, created_bool). admin_col_index is zero-based.
    """
    section("PART 1D — Ensure 'Admin Action Taken' column")
    if not values:
        log("  ✗ Sheet unreadable; cannot ensure column.")
        return None, False

    headers = [_norm(h) for h in values[0]]
    if ADMIN_ACTION_HEADER in headers:
        idx = headers.index(ADMIN_ACTION_HEADER)
        log(f"  ✓ Column already exists at {_col_letter(idx)} (index {idx}).")
        return idx, False

    # Find last non-empty header to place the new header right after it.
    last_nonempty = -1
    for i, h in enumerate(headers):
        if h:
            last_nonempty = i
    target_idx = last_nonempty + 1
    col = _col_letter(target_idx)
    log(f"  Column missing. Adding '{ADMIN_ACTION_HEADER}' header at {col}1 (index {target_idx}).")
    write_cell(col, 1, ADMIN_ACTION_HEADER)
    return target_idx, True


# ── DM sending (to the region's Ops owner) ───────────────────────────────────
def _send_dm_post_via_bot(receive_id, title, content_lines, receive_id_type="open_id"):
    """Send a Lark post DM to the region Ops owner using bot identity.

    ``receive_id`` must match ``receive_id_type`` exactly:
    - ``open_id`` for an app/bot-domain user open_id (ou_...)
    - ``chat_id`` for a confirmed P2P chat_id (oc_...)
    """
    from copyright_alert.bot_runtime import _post_api  # lazy import (avoid cycle)

    content = []
    for line in content_lines:
        if line == "__HR__":
            content.append([{"tag": "hr"}])
        elif isinstance(line, tuple) and line[0] == "link":
            content.append([{"tag": "a", "text": line[1], "href": line[2]}])
        else:
            content.append([{"tag": "text", "text": str(line)}])
    payload = {"zh_cn": {"title": title, "content": content}}
    try:
        resp = _post_api(
            f"/im/v1/messages?receive_id_type={receive_id_type}",
            {"receive_id": receive_id, "msg_type": "post",
             "content": json.dumps(payload, ensure_ascii=False)},
        )
        ok = resp.get("code") == 0
        log(f"  DM send via {receive_id_type} ({receive_id}) code={resp.get('code')} msg={resp.get('msg')}")
        return ok
    except Exception as exc:
        log(f"  ✗ DM send via {receive_id_type} failed: {exc!r}")
        return False


def send_dm_post(title, content_lines):
    """Send a Lark post (rich text) DM to the region's Ops owner.

    Routes by confirmed P2P chat_id when available, then by bot-domain open_id,
    otherwise falls back to the feishu-im-send path keyed on RECIPIENT_EMAIL
    (BR default).
    """
    if RECIPIENT_CHAT_ID:
        return _send_dm_post_via_bot(RECIPIENT_CHAT_ID, title, content_lines, "chat_id")
    if RECIPIENT_OPEN_ID:
        return _send_dm_post_via_bot(RECIPIENT_OPEN_ID, title, content_lines, "open_id")
    content = []
    for line in content_lines:
        if line == "__HR__":
            content.append([{"tag": "hr"}])
        elif isinstance(line, tuple) and line[0] == "link":
            content.append([{"tag": "a", "text": line[1], "href": line[2]}])
        else:
            content.append([{"tag": "text", "text": str(line)}])
    payload = {"zh_cn": {"title": title, "content": content}}
    msg_json = json.dumps(payload, ensure_ascii=False)
    cmd = ["python3", "scripts/im_send.py", "send", RECIPIENT_EMAIL, "post", msg_json]
    res = subprocess.run(cmd, cwd=str(FEISHU_IM_DIR), capture_output=True, text=True, timeout=90)
    ok = res.returncode == 0 and "RESULT" in (res.stdout + res.stderr) or res.returncode == 0
    log(f"  DM send rc={res.returncode}")
    log(f"    {(res.stdout + res.stderr).strip()[:400]}")
    return res.returncode == 0


def _row_lookup(headers):
    idx = {h: i for i, h in enumerate(headers) if h}
    return idx


def _cell(row, i):
    return _norm(row[i]) if i is not None and len(row) > i else ""


# ── PART 1B — remind about unselected statuses ───────────────────────────────
def remind_unselected_status(values):
    section("PART 1B — Remind about rows missing a Status")
    if not values or len(values) < 2:
        log("  No data rows.")
        return {"missing": 0, "dm_sent": False}

    headers = [_norm(h) for h in values[0]]
    idx = _row_lookup(headers)
    status_i = idx.get("Status")
    upc_i = idx.get("UPC")
    title_i = idx.get("Title")

    missing = []
    for r, row in enumerate(values[1:], start=2):
        # ignore fully empty trailing rows
        if not any(_norm(c) for c in row):
            continue
        status = _cell(row, status_i)
        if status == "":
            missing.append({
                "row": r,
                "upc": _cell(row, upc_i) or "N/A",
                "title": _cell(row, title_i) or "N/A",
            })

    log(f"  Rows missing Status: {len(missing)}")
    for m in missing:
        log(f"    • row {m['row']}: UPC {m['upc']} — {m['title']}")

    if not missing:
        log("  ✓ All rows have a Status. No reminder needed.")
        return {"missing": 0, "dm_sent": False}

    lines = [f"{len(missing)} row(s) in the copyright tracker still need a Status to be set:", "__HR__"]
    for m in missing:
        lines.append(f"• UPC {m['upc']} — {m['title']}  (row {m['row']})")
    lines.append("__HR__")
    lines.append(("link", "Open the tracker sheet", TRACKER_SHEET_URL))
    sent = send_dm_post("🟡 Copyright Tracker: rows missing a Status", lines)
    return {"missing": len(missing), "dm_sent": sent}


# ── PART 1C — action alert ───────────────────────────────────────────────────
def action_alert(values, admin_col_index):
    section("PART 1C — Action alert (takedown / assert needed on admin)")
    if not values or len(values) < 2:
        log("  No data rows.")
        return {"takedown": 0, "assert": 0, "dm_sent": False}

    headers = [_norm(h) for h in values[0]]
    idx = _row_lookup(headers)
    status_i = idx.get("Status")
    upc_i = idx.get("UPC")
    title_i = idx.get("Title")
    admin_i = admin_col_index if admin_col_index is not None else idx.get(ADMIN_ACTION_HEADER)

    need_takedown = []
    need_assert = []
    for r, row in enumerate(values[1:], start=2):
        if not any(_norm(c) for c in row):
            continue
        status = _cell(row, status_i)
        admin_done = _cell(row, admin_i)
        if _admin_action_has_real_value(admin_done):
            continue
        entry = {"row": r, "upc": _cell(row, upc_i) or "N/A", "title": _cell(row, title_i) or "N/A"}
        if _is_confirm_takedown_status(status):
            need_takedown.append(entry)
        elif _is_resolved_status(status):
            need_assert.append(entry)

    log(f"  Needs takedown on admin: {len(need_takedown)}")
    for e in need_takedown:
        log(f"    • row {e['row']}: UPC {e['upc']} — {e['title']}")
    log(f"  Needs 'Assert' on admin: {len(need_assert)}")
    for e in need_assert:
        log(f"    • row {e['row']}: UPC {e['upc']} — {e['title']}")

    if not need_takedown and not need_assert:
        log("  ✓ No outstanding admin actions.")
        return {"takedown": 0, "assert": 0, "dm_sent": False}

    lines = ["The following tracker rows still need action on the music admin:", "__HR__"]
    if need_takedown:
        lines.append(f"🔴 Needs takedown on admin ({len(need_takedown)}):")
        for e in need_takedown:
            lines.append(f"• UPC {e['upc']} — {e['title']}  (row {e['row']})")
        lines.append("__HR__")
    if need_assert:
        lines.append(f"✅ Needs 'Assert' marked on admin ({len(need_assert)}):")
        for e in need_assert:
            lines.append(f"• UPC {e['upc']} — {e['title']}  (row {e['row']})")
        lines.append("__HR__")
    lines.append(("link", "Open the tracker sheet", TRACKER_SHEET_URL))
    sent = send_dm_post("🔔 Copyright Tracker: admin actions needed", lines)
    return {"takedown": len(need_takedown), "assert": len(need_assert), "dm_sent": sent}


# ── PART 1E / 1F helpers (Spotify reply workflow) ────────────────────────────
def _header_index(headers, name):
    for idx, h in enumerate(headers):
        if _norm(h) == name:
            return idx
    return None


# C4: The duplicate `_cell(row, idx)` definition that previously lived here was
# removed. It silently shadowed the identical `_cell(row, i)` defined earlier in
# this module (same behavior/signature). The single canonical definition above
# is used by all callers.
def ensure_spotify_columns(values):
    """Make sure the 'Card Message ID' and 'Email Status' headers exist.

    Returns (card_idx, email_idx) header indices (creating headers if missing).
    'Detected At' is intentionally NOT created — the reliable ISO 'Date Received'
    column is reused as the detection timestamp.
    """
    headers = [_norm(h) for h in (values[0] if values else [])]
    card_idx = _header_index(headers, CARD_MSG_ID_HEADER)
    email_idx = _header_index(headers, EMAIL_STATUS_HEADER)
    next_col = len(headers)
    if card_idx is None:
        card_idx = next_col
        write_cell(_col_letter(card_idx), 1, CARD_MSG_ID_HEADER)
        log(f"  + Created '{CARD_MSG_ID_HEADER}' column at {_col_letter(card_idx)}")
        next_col += 1
    if email_idx is None:
        email_idx = next_col
        write_cell(_col_letter(email_idx), 1, EMAIL_STATUS_HEADER)
        log(f"  + Created '{EMAIL_STATUS_HEADER}' column at {_col_letter(email_idx)}")
    return card_idx, email_idx


def _load_posted_claims_map():
    """Map group-card message_id -> {source_email_message_id, claimant_email, claimant_name}.

    Handles both posted-claims schemas:
      - message_id / source_email_message_id (live + AP backfill)
      - posted_message_id / source_message_id (older US backfill)
    """
    out = {}
    for path in POSTED_CLAIMS_FILES:
        p = ROOT / path
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"  ⚠ Could not read {path}: {exc!r}")
            continue
        for rec in data.values():
            if not isinstance(rec, dict):
                continue
            card_id = rec.get("message_id") or rec.get("posted_message_id")
            if not card_id:
                continue
            out[_norm(card_id)] = {
                "source_email_message_id": rec.get("source_email_message_id") or rec.get("source_message_id") or "",
                "claimant_email": rec.get("claimant_email", "N/A"),
                "claimant_name": rec.get("claimant_name", "N/A"),
            }
    return out


def _load_dm_state():
    p = ROOT / SPOTIFY_DM_STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_dm_state(state):
    try:
        _atomic_write_json(ROOT / SPOTIFY_DM_STATE_FILE, state, ensure_ascii=False, indent=2)
    except Exception as exc:
        log(f"  ⚠ Could not persist DM state: {exc!r}")


def dm_action_cards(values):
    """PART 1E — DM action cards for day-1+ open cases.

    For each tracker row that is open/investigating, has no Email Status yet, and
    was detected at least 1 calendar day ago, send filipe.cairo a private DM
    action card. De-duped to at most once per card per calendar day.
    """
    section("PART 1E — DM ACTION CARDS (day-1+ open cases)")
    if not values:
        log("  No sheet data; skipping.")
        return {"sent": 0, "candidates": 0}

    headers = [_norm(h) for h in values[0]]
    idx = {
        "upc": _header_index(headers, UPC_HEADER),
        "isrc": _header_index(headers, ISRC_HEADER),
        "title": _header_index(headers, TITLE_HEADER),
        "artist": _header_index(headers, ARTIST_HEADER),
        "claimant": _header_index(headers, CLAIMANT_HEADER),
        "dsp": _header_index(headers, DSP_HEADER),
        "spotify_ref": _header_index(headers, SPOTIFY_REF_HEADER),
        "status": _header_index(headers, STATUS_HEADER),
        "date": _header_index(headers, DATE_RECEIVED_HEADER),
        "lark_msg": _header_index(headers, LARK_MSG_ID_HEADER),
        "card_msg": _header_index(headers, CARD_MSG_ID_HEADER),
        "email_status": _header_index(headers, EMAIL_STATUS_HEADER),
        "admin_action": _header_index(headers, ADMIN_ACTION_HEADER),
    }

    posted_map = _load_posted_claims_map()
    state = _load_dm_state()
    today = date.today()
    today_str = today.isoformat()

    sent = 0
    candidates = 0
    skipped_no_src = 0
    for row_num, row in enumerate(values[1:], start=2):
        status = _cell(row, idx["status"])
        admin_action = _cell(row, idx["admin_action"])
        if not _is_open_for_ops(status, admin_action):
            log(f"  • Skipping row {row_num} ({_cell(row, idx['upc']) or 'N/A'}): status={status!r}, admin_action={admin_action!r}")
            continue
        if _cell(row, idx["email_status"]):  # already replied
            continue

        detected_raw = _cell(row, idx["date"])
        detected = parse_detected_date(detected_raw)
        if detected is None:
            continue
        if (today - detected).days < 1:  # not yet 1 calendar day old
            continue

        card_msg_id = _cell(row, idx["card_msg"]) or _cell(row, idx["lark_msg"])
        if not card_msg_id:
            continue
        candidates += 1

        # De-dupe: at most one DM per card per day.
        if state.get(card_msg_id) == today_str:
            continue

        extra = posted_map.get(card_msg_id, {})
        source_email_message_id = extra.get("source_email_message_id", "")
        if not source_email_message_id:
            skipped_no_src += 1
            log(f"  ⚠ Row {row_num} ({_cell(row, idx['upc'])}): no source email message_id "
                f"in posted_claims; cannot reply, skipping DM.")
            continue

        case = {
            "upc": _cell(row, idx["upc"]) or "N/A",
            "isrc": _cell(row, idx["isrc"]) or "N/A",
            "title": _cell(row, idx["title"]) or "N/A",
            "artist": _cell(row, idx["artist"]) or "N/A",
            "claimant_name": _cell(row, idx["claimant"]) or extra.get("claimant_name", "N/A"),
            "claimant_email": extra.get("claimant_email", "N/A"),
            # DSP shown on the private action card (tracker column I), falling
            # back to the posted_claims record when the tracker cell is blank.
            "dsp": _cell(row, idx["dsp"]) or extra.get("dsp", "N/A"),
            # Spotify ref code read back from the tracker column (populated at
            # ingest time), falling back to the posted_claims record. Used as the
            # button payload ref_id so replies thread to the right claim.
            "ref_id": _cell(row, idx["spotify_ref"]) or extra.get("ref_id", ""),
            "source_email_message_id": source_email_message_id,
            "lark_card_message_id": card_msg_id,
            "detected_at": detected_raw,
            "tracker_row": row_num,
            # Route the private action card to this region's Ops owner.
            "region": ACTIVE_REGION,
            "ops_dm_email": RECIPIENT_EMAIL,
            "ops_dm_open_id": RECIPIENT_OPEN_ID,
            "ops_dm_chat_id": RECIPIENT_CHAT_ID,
        }
        try:
            if send_dm_action_card(case):
                sent += 1
                state[card_msg_id] = today_str
        except Exception as exc:
            log(f"  ✗ DM send error for row {row_num}: {exc!r}")

    _save_dm_state(state)
    log(f"  DM action cards: {sent} sent / {candidates} eligible "
        f"({skipped_no_src} skipped for missing source email).")
    return {"sent": sent, "candidates": candidates, "skipped_no_source": skipped_no_src}


def _get_card_content(message_id):
    """GET the current interactive card body for an existing message."""

    def make_request():
        token = _get_bot_access_token()
        if not token:
            raise RuntimeError("Could not get bot access token")
        url = f"https://open.larksuite.com/open-apis/im/v1/messages/{message_id}"
        return urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"Bearer {token}"})

    try:
        data = request_json_with_auth_retry(make_request, timeout=30, context=f"daily_workflow._get_card_content:{message_id}")
        items = (data.get("data") or {}).get("items") or []
        if not items:
            return None
        body = items[0].get("body") or {}
        content = body.get("content")
        if not content:
            return None
        return json.loads(content)
    except Exception as exc:
        log(f"  ⚠ Could not fetch card {message_id}: {exc!r}")
        return None


_COUNTDOWN_MARK = "⏳ **Spotify reply countdown:**"


def _countdown_text(days_remaining):
    if days_remaining > 1:
        badge = f"🟢 {days_remaining} days left"
    elif days_remaining == 1:
        badge = "🟡 1 day left"
    elif days_remaining == 0:
        badge = "🟠 Due today"
    else:
        badge = f"🔴 Overdue by {abs(days_remaining)} day(s)"
    return f"{_COUNTDOWN_MARK} {badge}"


def build_card_with_countdown(card, days_remaining):
    """Insert/update a countdown badge element at the top of an existing card,
    keeping the rest of the card structure intact."""
    if not isinstance(card, dict):
        return card
    card.setdefault("config", {})["update_multi"] = True
    elements = card.get("elements")
    if not isinstance(elements, list):
        return card
    countdown_el = {"tag": "div", "text": {"tag": "lark_md", "content": _countdown_text(days_remaining)}}
    # Update in place if a countdown element already exists.
    for el in elements:
        try:
            txt = (el.get("text") or {}).get("content", "")
        except AttributeError:
            txt = ""
        if isinstance(txt, str) and txt.startswith(_COUNTDOWN_MARK):
            el["text"]["content"] = _countdown_text(days_remaining)
            return card
    elements.insert(0, countdown_el)
    return card


def _reconstruct_card_from_row(row, idx, message_id="", region=""):
    """Rebuild a valid interactive card from the tracker-sheet row.

    Fallback for cards posted before card-persistence existed. The GET messages
    API only returns Lark's rendered post-format ({title, elements:[[...]]}),
    which is NOT patchable, so we rebuild the canonical card shape via
    run_alert.build_card from the data we logged in the sheet. Fields not stored
    in the sheet (claimant company/email/message, mention user-ids) degrade to
    "N/A"/plain text, but the card stays structurally valid and patchable.

    ``region`` (optional) pins the card's region context so a rebuilt US/SPLA/BR
    card keeps its own PoC/ops routing instead of the caller's global
    CURRENT_REGION. Callers that already configured the region (e.g. the
    countdown refresh) can omit it and build_card falls back to CURRENT_REGION.
    """
    def cell(key):
        i = idx.get(key)
        return _cell(row, i) if i is not None else ""

    artists = cell("artist") or ""
    ef = {
        "title": cell("title") or "N/A",
        "upc": cell("upc") or "N/A",
        "isrc": cell("isrc") or "N/A",
        "email_source": cell("email_source") or "N/A",
        "claimant_name": cell("claimant") or "N/A",
        "claimant_company": "N/A",
        "claimant_email": "N/A",
        "claimant_message": "N/A",
        "dsp": cell("dsp") or "N/A",
        "date_received": cell("date") or "N/A",
        "ref_id": cell("spotify_ref") or "N/A",
    }
    ar = {
        "album_title": cell("title") or "N/A",
        "uid": cell("uid") or "",
        "display_artist": json.dumps(
            [a.strip() for a in artists.split(",") if a.strip()], ensure_ascii=False),
        "bd_manager_list": None,
        "operation_manager_list": None,
    }
    status = cell("status") or ""
    return build_card(ef, ar, current_status=status, lark_message_id=message_id, region=region)


def _sync_replacement_message_id(old_message_id, new_message_id, card, *, row_num=None, idx=None):
    """Persist a replacement group-card message ID after lark-cli resend fallback."""
    if not old_message_id or not new_message_id:
        return
    save_posted_card(new_message_id, card)

    try:
        state = _load_dm_state()
        if old_message_id in state and new_message_id not in state:
            state[new_message_id] = state[old_message_id]
            _save_dm_state(state)
    except Exception as exc:
        log(f"  ⚠ Could not sync DM state for replacement {old_message_id} → {new_message_id}: {exc!r}")

    try:
        posted = _load_posted_claims()
        changed = False
        for payload in posted.values():
            if isinstance(payload, dict) and payload.get("message_id") == old_message_id:
                payload["message_id"] = new_message_id
                changed = True
        if changed:
            _atomic_write_json(POSTED_CLAIMS_FILE, posted, ensure_ascii=False, indent=2)
    except Exception as exc:
        log(f"  ⚠ Could not sync posted_claims for replacement {old_message_id} → {new_message_id}: {exc!r}")

    if row_num and idx:
        try:
            from copyright_alert.handle_callback import _col_letter, _write_sheet_cell_cli
            for key in ("card_msg", "lark_msg"):
                col_idx = idx.get(key)
                if col_idx is None:
                    continue
                cell = f"{_col_letter(col_idx)}{row_num}"
                _write_sheet_cell_cli(TRACKER_SHEET_URL, TRACKER_SHEET_ID, cell, new_message_id)
        except Exception as exc:
            log(f"  ⚠ Could not sync tracker row {row_num} with replacement ID {new_message_id}: {exc!r}")


def _replace_card_message_via_lark_cli(old_message_id, card, *, row_num=None, idx=None):
    """Fallback for countdown refresh when PATCH is unavailable in cron contexts.

    Despite the historical function name, use the repo-local bot credentials
    first. The AIME lark-cli bot identity may resolve to a different app than
    the copyright bot, which can produce false ``230002`` (bot not in chat)
    errors for regional groups such as SPLA even when the production bot is in
    the group. ``run_alert.post_card`` already uses the production bot first and
    only falls back to lark-cli if local credentials are unavailable.
    """
    from copyright_alert import run_alert as ra

    # H2 (door #1): snapshot the intended destination + region at call time and
    # pass them explicitly. Previously this called ra.post_card(card) with no
    # aeolus_row, which skipped the region guard entirely and posted to whatever
    # the process-global TARGET_CHAT_ID happened to hold — if another thread had
    # reconfigured the region mid-refresh, the replacement card landed in the
    # wrong group. Pinning chat_id + expected_region enforces the guard and
    # posts to the correct group regardless of concurrent reconfiguration.
    target_chat_id = TARGET_CHAT_ID
    ok, new_message_id = ra.post_card(
        card,
        chat_id=target_chat_id,
        expected_region=ACTIVE_REGION,
        context=f"{ACTIVE_REGION} countdown replacement card",
    )
    if not ok or not new_message_id:
        raise RuntimeError(f"Replacement send failed for chat_id={target_chat_id}")
    _sync_replacement_message_id(old_message_id, new_message_id, card, row_num=row_num, idx=idx)
    return new_message_id


def countdown_refresh(values):
    """PART 1F — Refresh the daily countdown badge on each open group card."""
    section("PART 1F — COUNTDOWN CARD REFRESH")
    if not values:
        log("  No sheet data; skipping.")
        return {"refreshed": 0}

    headers = [_norm(h) for h in values[0]]
    idx = {
        "status": _header_index(headers, STATUS_HEADER),
        "date": _header_index(headers, DATE_RECEIVED_HEADER),
        "lark_msg": _header_index(headers, LARK_MSG_ID_HEADER),
        "card_msg": _header_index(headers, CARD_MSG_ID_HEADER),
        "email_status": _header_index(headers, EMAIL_STATUS_HEADER),
        "upc": _header_index(headers, UPC_HEADER),
        "isrc": _header_index(headers, ISRC_HEADER),
        "title": _header_index(headers, TITLE_HEADER),
        "artist": _header_index(headers, ARTIST_HEADER),
        "claimant": _header_index(headers, CLAIMANT_HEADER),
        "email_source": _header_index(headers, "Email Source"),
        "dsp": _header_index(headers, "DSP"),
        "spotify_ref": _header_index(headers, SPOTIFY_REF_HEADER),
        "uid": _header_index(headers, "UID"),
        "admin_action": _header_index(headers, ADMIN_ACTION_HEADER),
    }
    today = date.today()
    refreshed = 0
    attempted = 0
    for row_num, row in enumerate(values[1:], start=2):
        status = _cell(row, idx["status"])
        admin_action = _cell(row, idx["admin_action"])
        if not _is_open_for_ops(status, admin_action):
            continue
        if _cell(row, idx["email_status"]):  # already handled
            continue
        card_msg_id = _cell(row, idx["card_msg"]) or _cell(row, idx["lark_msg"])
        if not card_msg_id:
            continue
        detected = parse_detected_date(_cell(row, idx["date"]))
        if detected is None:
            continue
        days_remaining = business_days_remaining_brt(detected)
        attempted += 1
        # Prefer the exact persisted card (lossless). The GET messages API only
        # returns Lark's rendered post-format, which is NOT patchable and was the
        # cause of the HTTP 400s, so we never patch that. For legacy cards with no
        # persisted copy, rebuild a valid card from the tracker row.
        card = load_posted_card(card_msg_id)
        if not card:
            card = _reconstruct_card_from_row(row, idx, card_msg_id)
        if not card:
            continue
        patched = build_card_with_countdown(card, days_remaining)
        try:
            ok = patch_card_message(card_msg_id, patched)
            if ok:
                refreshed += 1
                continue
            replacement_id = _replace_card_message_via_lark_cli(card_msg_id, patched, row_num=row_num, idx=idx)
            log(f"  ✓ Countdown replacement posted for row {row_num}: {card_msg_id} → {replacement_id}")
            refreshed += 1
        except Exception as exc:
            log(f"  ✗ Countdown patch error for {card_msg_id}: {exc!r}")
    log(f"  Countdown refresh: {refreshed} updated / {attempted} open cards.")
    return {"refreshed": refreshed, "attempted": attempted}


# ── Main ─────────────────────────────────────────────────────────────────────
def main(region=None):
    if region:
        configure_region(region)
    log_path = _setup_logging()
    started = datetime.now()
    log(f"Copyright alert daily workflow — started {started.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Region: {ACTIVE_REGION}")
    log(f"Log file: {log_path}")
    log(f"Mailbox: {MAILBOX} | Alert group: {TARGET_CHAT_ID}")
    log(f"Tracker: {TRACKER_SHEET_URL}")
    log(f"Ops DM owner: {RECIPIENT_OPEN_ID or RECIPIENT_EMAIL}")
    log(f"Checkpoint: {CHECKPOINT_FILE}")

    results = {}

    # A) Incremental scan
    try:
        results["scan"] = run_scan()
    except Exception as e:
        log(f"  ✗ Scan section error: {e!r}")
        results["scan"] = {"error": repr(e)}

    # Sheet sections — ensure column first so B/C reads are accurate (D).
    values = read_sheet_values("A:Z")
    try:
        admin_idx, created = ensure_admin_action_column(values)
        results["admin_column"] = {"index": admin_idx, "created": created}
        if created:
            # re-read so the header row reflects the new column for B/C
            values = read_sheet_values("A:Z")
    except Exception as e:
        log(f"  ✗ Admin column section error: {e!r}")
        results["admin_column"] = {"error": repr(e)}
        admin_idx = None

    # B) Remind about unselected statuses
    try:
        results["unselected"] = remind_unselected_status(values)
    except Exception as e:
        log(f"  ✗ Unselected-status section error: {e!r}")
        results["unselected"] = {"error": repr(e)}

    # C) Action alert
    try:
        results["action_alert"] = action_alert(values, results.get("admin_column", {}).get("index"))
    except Exception as e:
        log(f"  ✗ Action-alert section error: {e!r}")
        results["action_alert"] = {"error": repr(e)}

    # Ensure the Spotify reply columns exist before E/F read them.
    try:
        ensure_spotify_columns(values)
        # re-read so any newly-created headers are reflected for E/F
        values = read_sheet_values("A:Z")
    except Exception as e:
        log(f"  ✗ Spotify column section error: {e!r}")

    # E) DM action cards for day-1+ open cases
    try:
        results["dm_action_cards"] = dm_action_cards(values)
    except Exception as e:
        log(f"  ✗ DM action-card section error: {e!r}")
        results["dm_action_cards"] = {"error": repr(e)}

    # F) Countdown card refresh
    try:
        results["countdown_refresh"] = countdown_refresh(values)
    except Exception as e:
        log(f"  ✗ Countdown-refresh section error: {e!r}")
        results["countdown_refresh"] = {"error": repr(e)}

    section("RUN COMPLETE")
    log(json.dumps(results, ensure_ascii=False, indent=2))
    finished = datetime.now()
    log(f"\nFinished {finished.strftime('%Y-%m-%d %H:%M:%S')} (took {(finished - started).seconds}s)")
    return results


if __name__ == "__main__":
    # Region may be passed as argv[1] (e.g. "SPLA") or via COPYRIGHT_REGION env.
    _region = None
    if len(sys.argv) > 1 and sys.argv[1].strip():
        _region = sys.argv[1].strip()
    elif os.environ.get("COPYRIGHT_REGION"):
        _region = os.environ["COPYRIGHT_REGION"].strip()
    main(_region)
