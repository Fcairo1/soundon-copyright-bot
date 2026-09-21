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
from copyright_alert.major_label_detector import MAJOR_LABEL_HEADERS
from copyright_alert.paths import inner_skill

# ── Make the copyright_alert package importable & anchor relative paths ───────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # run_alert helpers use relative paths like "copyright_alert/..."

from copyright_alert.state_io import update_json_state  # noqa: E402
from copyright_alert.run_alert import (  # noqa: E402
    MAILBOX,
    TARGET_CHAT_ID,
    TRACKER_SHEET_URL,
    TRACKER_SHEET_ID,
    TRIAGE_QUERY,
    parse_lark_json,
    fetch_email,
    extract_claim_entries,
    extract_retraction_fields,
    is_retraction_email,
    is_retraction_already_processed,
    _save_retracted_claim,
    batch_query_aeolus_by_upc,
    enrich_with_engagement_once,
    qualifies,
    claim_key,
    is_claim_already_posted,
    _save_posted_claim,
    _delete_posted_claim,
    _reserve_claim_before_post,
    _atomic_write_json,
    _load_posted_claims,
    POSTED_CLAIMS_FILE,
    RETRACTION_SENDER,
    RETRACTION_SIGNAL_PHRASE,
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
TRIAGE_MAX = 50            # messages fetched per triage page (page size)
# Absolute ceiling on how many messages a single scan will pull while paginating
# back to the previous checkpoint. Bounds the work per run so a runaway inbox
# cannot make one scan fetch unboundedly, while still being large enough to span
# any realistic backlog between two daily runs. The +triage CLI caps --max at
# 400, so this is also the practical maximum reach of one scan.
TRIAGE_HARD_CAP = 400
# Dedicated secondary inbox fetch for Spotify metadata / misrepresentation
# notices. These emails do not consistently use the standard "Infringement
# Claim" subject, so the daily scan must search additional keywords and merge the
# resulting messages before routing.
METADATA_NOTICE_QUERIES = [
    "misrepresent",
    "Content Protection",
    "metadata",
    "Spotify Content Protection",
    "misleading",
]
# G2: A failed message is retried at most this many times before it is dropped
# from the retry list, so a message that will never succeed is not re-processed
# on every run forever.
MAX_RETRY_ATTEMPTS = 5
# Lark only allows interactive-message PATCH within 14 days. Do not create
# replacement group cards for older claims — that creates duplicate alerts for
# already-notified UPCs. Expired cards are left in place and only newer cards
# remain eligible for daily countdown PATCH refreshes.
MAX_LARK_CARD_PATCH_AGE_DAYS = 14
ACTIVE_REGION = "BR"  # region this workflow run is configured for
RECIPIENT_EMAIL = "filipe.cairo@bytedance.com"  # filipe.cairo — personal alert DM target (BR default)
RECIPIENT_OPEN_ID = ""  # when set, ops DMs go to this open_id via the copyright bot
RECIPIENT_CHAT_ID = ""  # optional confirmed DM chat_id for the ops owner
FEISHU_IM_DIR = inner_skill("feishu-im-send")
ADMIN_ACTION_HEADER = "Admin Action Taken"
RETRACTED_HEADER = "Retracted"
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
# J2: The live tracker header for this column is literally "ref_code" (not
# "Spotify Ref Code"), so the readback lookup must match that exact header or
# column U can never be resolved and `ef["ref_id"]` stays blank on rebuilds.
SPOTIFY_REF_HEADER = "ref_code"
POSTED_CLAIMS_FILES = [
    "copyright_alert/posted_claims.json",
    "copyright_alert/posted_claims_ap_direitos_br.json",
]
POSTED_CLAIMS_FALLBACK_FILES = [
    "runtime/posted_claims.json",
]
SPOTIFY_DM_STATE_FILE = "copyright_alert/spotify_dm_sent.json"
# C1: the reply deadline is unified to 5 BUSINESS days in BRT. The single source
# of truth lives in tag_managers; do not reintroduce a local calendar-day value.
from copyright_alert.tag_managers import business_days_remaining_brt, REPLY_DEADLINE_WORKDAYS  # noqa: E402
from copyright_alert import metadata_notice  # noqa: E402
from copyright_alert import takedown_stream_tracker  # noqa: E402
from copyright_alert import claim_events  # noqa: E402


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
def load_checkpoint_state():
    if not os.path.exists(CHECKPOINT_FILE):
        return {}
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"  ⚠ Could not read checkpoint: {e!r}")
        return {}


def load_checkpoint():
    return load_checkpoint_state().get("last_message_id")


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
def _parse_iso_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mail_search_timestamp(value):
    parsed = _parse_iso_datetime(value)
    return parsed.isoformat(timespec="seconds") if parsed is not None else None


def search_inbox_messages(query, *, sender=None, start_time=None, end_time=None, limit=None):
    query = str(query or "").strip()[:50]
    if not query:
        return []

    items = []
    page_token = ""
    while True:
        params = {"user_mailbox_id": "me", "page_size": 15}
        if page_token:
            params["page_token"] = page_token

        filter_payload = {}
        if sender:
            filter_payload["from"] = [str(sender).strip()]
        create_time = {}
        start_iso = _mail_search_timestamp(start_time)
        end_iso = _mail_search_timestamp(end_time)
        if start_iso:
            create_time["start_time"] = start_iso
        if end_iso:
            create_time["end_time"] = end_iso
        if create_time:
            filter_payload["create_time"] = create_time

        data = {"query": query, "filter": filter_payload}
        cmd = [
            "lark-cli", "mail", "user_mailboxes", "search",
            "--params", json.dumps(params, ensure_ascii=False),
            "--data", json.dumps(data, ensure_ascii=False),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(f"mail search failed rc={res.returncode}: {(res.stdout + res.stderr)[:800]}")
        parsed = parse_lark_json(res.stdout)
        if not parsed:
            raise RuntimeError(f"mail search returned unreadable output: {res.stdout[:500]}")

        for item in parsed.get("items") or []:
            meta = item.get("meta_data") if isinstance(item.get("meta_data"), dict) else {}
            message_id = meta.get("message_biz_id") or item.get("id")
            if not message_id:
                continue
            items.append({
                "message_id": str(message_id).strip(),
                "subject": str(meta.get("title") or "").strip(),
                "date": str(meta.get("create_time") or "").strip(),
                "thread_id": str(meta.get("thread_id") or message_id).strip(),
            })
            if limit and len(items) >= limit:
                return items[:limit]

        if not parsed.get("has_more"):
            break
        page_token = str(parsed.get("page_token") or "").strip()
        if not page_token:
            break
    return items


def fetch_messages_raw(checkpoint=None):
    """Fetch inbox messages newest-first in a SINGLE triage call.

    ROOT-CAUSE FIX (the "31 missing early-July BR cases" incident):

    The previous implementation issued a single ``mail +triage --max 50`` call
    and returned only the 50 newest messages. The caller then *unconditionally*
    advanced the checkpoint to the newest message (``messages[0]``) at the end of
    every run. When more than 50 qualifying emails arrived between two daily
    runs, the previous checkpoint fell *outside* that 50-message window, the scan
    loop never reached it (``stopped_at_checkpoint`` stayed False), yet the
    checkpoint still jumped forward to the newest message. Every email sitting
    between the 50th-newest and the old checkpoint was therefore never scanned
    and was permanently leapfrogged — exactly what happened to the 31 early-July
    claims.

    J3 — the earlier ``--page-token`` pagination loop was INERT: lark-cli
    ``mail +triage`` has no next-page support, so it silently ended after page 1
    anyway. We now issue a single triage call sized to the run type:

    * Incremental runs (a ``checkpoint`` exists): fetch in ONE call using
      ``--max TRIAGE_HARD_CAP`` (400), the practical maximum ``+triage`` allows.
      This makes the fetch window span far enough back to reach the previous
      checkpoint in all realistic backlogs.
    * First run (``checkpoint is None``): fetch a single page of ``TRIAGE_MAX``
      (50) to avoid scanning a huge historical backlog on cold start.

    If the checkpoint is still not inside the fetched window, ``run_scan`` keeps
    the checkpoint (does NOT advance it) and logs the 🚨 warning, so mail between
    the fetch window and the old checkpoint is never permanently orphaned.
    """
    max_fetch = TRIAGE_MAX if checkpoint is None else TRIAGE_HARD_CAP
    all_messages = []
    seen_ids = set()

    def _fetch_messages(query, is_main=False):
        cmd = [
            "lark-cli", "mail", "+triage",
            "--mailbox", MAILBOX,
            "--query", query,
            "--max", str(max_fetch),
            "--format", "json",
        ]
        label = "main" if is_main else "metadata"
        checkpoint_hint = f", checkpoint={checkpoint or '(none)'}" if is_main else ""
        log(f"Fetching inbox ({label}): query={query!r}, max={max_fetch}, mailbox={MAILBOX}{checkpoint_hint}")

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if res.returncode != 0:
            log(f"  ✗ {label} triage failed rc={res.returncode}: {(res.stdout + res.stderr)[:200]}")
            return []

        parsed = parse_lark_json(res.stdout)
        if not parsed:
            log(f"  ✗ Failed to parse {label} triage output.")
            return []

        data = parsed.get("data") or {}
        return parsed.get("messages") or data.get("messages") or []

    # 1. Main fetch (Infringement Claim)
    for m in _fetch_messages(TRIAGE_QUERY, is_main=True):
        mid = m.get("message_id")
        if mid and mid not in seen_ids:
            all_messages.append(m)
            seen_ids.add(mid)

    # 2. Dedicated Metadata Notice fetch. Lark's triage query is a generic
    # keyword search across message text, so issue one fetch per keyword and merge
    # them as an OR-set.
    for metadata_query in METADATA_NOTICE_QUERIES:
        for m in _fetch_messages(metadata_query, is_main=False):
            mid = m.get("message_id")
            if mid and mid not in seen_ids:
                all_messages.append(m)
                seen_ids.add(mid)

    # Sort merged results by date descending so messages[0] is the newest (high-water mark)
    all_messages.sort(key=lambda x: x.get("date", ""), reverse=True)

    if checkpoint and any(m.get("message_id") == checkpoint for m in all_messages):
        log(f"  ✓ Reached previous checkpoint within merged fetch window "
            f"({len(all_messages)} unique message(s) fetched).")

    log(f"  Fetched {len(all_messages)} total unique emails (merged).")
    return all_messages


def _prefilter_skip_reason(subject, thread_id, seen_threads):
    if re.match(r"(?i)^(re:|fw:)", subject or ""):
        return "reply/forward subject"
    # Content ID / claim-release requests must stay in the normal alert flow;
    # extract_fields() tags likely release requests with a visible warning.
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

    # Spotify Metadata / Misrepresentation notices are handled COMPLETELY
    # differently from infringement claims: no group card, no tracker row — just a
    # private DM to the regional Ops owner (re-sent daily until Actioned). Route
    # them out of the infringement flow before any UPC/qualification logic.
    try:
        if metadata_notice.is_metadata_notice(body, subject, meta):
            log("     ⚠️ Spotify metadata/misrepresentation notice — routing to DM handler")
            result = metadata_notice.handle_metadata_notice(body, subject, meta, msg_id=msg_id)
            log(f"     metadata notice: {result}")
            return ("skip", "metadata notice")
    except Exception as exc:
        log(f"     ⚠ metadata-notice routing error (continuing as normal claim): {exc!r}")

    entries = extract_claim_entries(body, subject, meta)
    identifiers = []
    candidates = []
    for ef in entries:
        upc = str(ef.get("upc", "") or "").strip()
        isrc = ef.get("isrc", "")
        identifiers.append(f"{upc or 'N/A'}|{isrc or 'N/A'}")
        if (not upc or upc == "N/A") and (not isrc or isrc == "N/A"):
            continue
        candidates.append({
            "message_id": msg_id,
            "subject": subject,
            "date": date,
            "ef": ef,
        })

    log(f"     Extracted {len(candidates)} claim entr{'y' if len(candidates) == 1 else 'ies'}: {', '.join(identifiers) or 'N/A'}")
    if not candidates:
        log("     ✗ No UPC or ISRC, skipping")
        summary["skipped_no_identifier"] += 1
        return ("skip", "no identifier")

    return ("candidate", candidates)


# ── PART 1A — incremental scan + post ────────────────────────────────────────
def run_scan():
    section("PART 1A — Incremental inbox scan + post cards")
    # Load the checkpoint BEFORE fetching so the fetch can paginate back to it and
    # never leave a gap (root-cause fix for the 31 leapfrogged early-July cases).
    checkpoint = load_checkpoint()
    messages = fetch_messages_raw(checkpoint=checkpoint)
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

    # `checkpoint` was already loaded above (before the fetch) and reused here so
    # the fetch window and the Phase-1 stop condition agree on the same value.
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
            candidates.extend(info if isinstance(info, list) else [info])
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
            candidates.extend(info if isinstance(info, list) else [info])
        elif kind == "failed":
            failed_entries[msg_id] = {"message_id": msg_id, "subject": subject, "date": date}

    summary["parsed_candidates"] = len(candidates)
    aeolus_by_upc = batch_query_aeolus_by_upc([c["ef"].get("upc") for c in candidates])
    summary["unique_upcs"] = len(aeolus_by_upc)
    summary["engagement_upcs"] = 0

    from copyright_alert.run_alert import query_aeolus
    for c in candidates:
        msg_id = c["message_id"]
        subject = c["subject"]
        ef = c["ef"]
        upc = str(ef.get("upc", "") or "").strip()
        isrc = str(ef.get("isrc", "") or "").strip()
        ar = aeolus_by_upc.get(upc) or {}

        if not ar and (not upc or upc == "N/A") and isrc and isrc != "N/A":
            ar = query_aeolus(isrc, "isrc")
            if ar and ar.get("upc") and ar.get("upc") != "N/A":
                upc = str(ar.get("upc")).strip()
                ef["upc"] = upc

        ar = enrich_with_engagement_once(ar)

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
        if is_claim_already_posted(dup_key, ef=ef, ar=ar, subject=subject):
            log(f"     ✗ Duplicate already posted: {dup_key}")
            summary["skipped_duplicate"] += 1
            failed_entries.pop(msg_id, None)
            continue

        log(f"     ✓ Qualifies & new — posting card for UPC {upc} …")
        card = build_card(ef, ar, region=ACTIVE_REGION)
        with open(LAST_CARD_FILE, "w", encoding="utf-8") as fh:
            json.dump(card, fh, indent=2)

        _reserve_claim_before_post(dup_key, ef, ar, subject, msg_id)
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
                "user_name": ar.get("user_name", "N/A"),
                "ref_id": ef.get("ref_id", "N/A"),
                "claimant_name": ef.get("claimant_name", "N/A"),
                "claimant_email": ef.get("claimant_email", "N/A"),
                "possible_content_id_release_request": bool(ef.get("possible_content_id_release_request")),
                "possible_non_claim_notice": bool(ef.get("possible_non_claim_notice")),
                "region": ACTIVE_REGION,
                "tracker_row": tracker_row,
                "chat_id": TARGET_CHAT_ID,
            })
            summary["posted"] += 1
            failed_entries.pop(msg_id, None)
            log(f"     ✅ Posted card {posted_message_id} for UPC {upc}")
        else:
            _delete_posted_claim(dup_key)
            log("     ✗ Card posting failed for this candidate — will retry next run")
            failed_entries[msg_id] = {"message_id": msg_id, "subject": subject, "date": c.get("date", "")}

    if checkpoint and not summary["stopped_at_checkpoint"] and len(messages) >= TRIAGE_HARD_CAP:
        log(f"  🚨 WARNING: previous checkpoint {checkpoint} was NOT reached even after "
            f"fetching the {TRIAGE_HARD_CAP}-message hard cap in a single triage call. An "
            f"unusually large backlog has accumulated; emails older than this window may still "
            f"be unscanned. Raise TRIAGE_HARD_CAP or run a manual backfill.")
        # J3: Do NOT advance the checkpoint here — hold it at its previous value.
        # We fetched the full hard cap and still never reached the old checkpoint,
        # so mail between the fetch window and the old checkpoint has NOT been
        # scanned yet. Advancing to messages[0] would leapfrog and permanently
        # orphan those emails (the exact 31-case bug). Keeping the old checkpoint
        # lets the next run try again to close the gap.
        new_checkpoint = checkpoint
        log(f"  ↩ Holding checkpoint at {checkpoint} (not advancing) so unscanned mail is not orphaned.")

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


def _is_retracted_value(v):
    return _norm(v).casefold() in {"yes", "y", "true", "1"}


def _is_open_for_ops(status, admin_action="", retracted=""):
    if _is_retracted_value(retracted):
        return False
    normalized = _normalized_status(status)
    if normalized == "resolved":
        return False
    if normalized == "confirm takedown":
        return not _admin_action_has_real_value(admin_action)
    return normalized in {"", "investigating", "disputing", "pending", "open"}


def read_sheet_values(rng="A:AA"):
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


# ── PART 1D — ensure tracker columns ─────────────────────────────────────────
def ensure_tracker_column(values, header_name, *, section_title=None):
    if section_title:
        section(section_title)
    if not values:
        log("  ✗ Sheet unreadable; cannot ensure column.")
        return None, False

    headers = [_norm(h) for h in values[0]]
    if header_name in headers:
        idx = headers.index(header_name)
        log(f"  ✓ Column already exists at {_col_letter(idx)} (index {idx}).")
        return idx, False

    last_nonempty = -1
    for i, h in enumerate(headers):
        if h:
            last_nonempty = i
    target_idx = last_nonempty + 1
    col = _col_letter(target_idx)
    log(f"  Column missing. Adding '{header_name}' header at {col}1 (index {target_idx}).")
    write_cell(col, 1, header_name)
    return target_idx, True


def ensure_admin_action_column(values):
    """Ensure the tracker has an 'Admin Action Taken' header as the last column."""
    return ensure_tracker_column(values, ADMIN_ACTION_HEADER, section_title="PART 1D — Ensure 'Admin Action Taken' column")


def ensure_retracted_column(values):
    """Ensure the tracker has a 'Retracted' header available for claim retractions."""
    return ensure_tracker_column(values, RETRACTED_HEADER, section_title="PART 1A.1 — Ensure 'Retracted' column")


def ensure_major_label_columns(values):
    created = []
    indices = {}
    for header in MAJOR_LABEL_HEADERS:
        idx, was_created = ensure_tracker_column(values, header)
        indices[header] = idx
        if was_created:
            created.append(header)
            values = read_sheet_values("A:AF")
    if created:
        log(f"  ✓ Major label columns created: {', '.join(created)}")
    else:
        log("  ✓ Major label columns already exist.")
    return indices, created


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
    helper_script = FEISHU_IM_DIR / "scripts" / "im_send.py"
    if not helper_script.exists():
        log(f"  ⚠ DM helper not found at {helper_script}; skipping DM fallback.")
        return False
    cmd = ["python3", "scripts/im_send.py", "send", RECIPIENT_EMAIL, "post", msg_json]
    try:
        res = subprocess.run(cmd, cwd=str(FEISHU_IM_DIR), capture_output=True, text=True, timeout=90)
    except FileNotFoundError as exc:
        log(f"  ⚠ DM helper path unavailable at {FEISHU_IM_DIR}: {exc!r}")
        return False
    ok = res.returncode == 0 and "RESULT" in (res.stdout + res.stderr) or res.returncode == 0
    log(f"  DM send rc={res.returncode}")
    log(f"    {(res.stdout + res.stderr).strip()[:400]}")
    return res.returncode == 0


def _row_lookup(headers):
    idx = {h: i for i, h in enumerate(headers) if h}
    return idx


def _cell(row, i):
    return _norm(row[i]) if i is not None and len(row) > i else ""


def _format_retraction_date(value):
    parsed = _parse_iso_datetime(value)
    if parsed is not None:
        return parsed.date().isoformat()
    text = _norm(value)
    return text.split("T", 1)[0] if "T" in text else (text or "Unknown date")


def _set_row_value(values, row_num, col_index, value):
    if row_num < 1 or col_index is None or row_num > len(values):
        return
    row = values[row_num - 1]
    while len(row) <= col_index:
        row.append("")
    row[col_index] = value



def match_tracker_row_for_retraction(values, retraction_fields):
    if not values or len(values) < 2:
        return None
    headers = [_norm(h) for h in values[0]]
    idx = _row_lookup(headers)
    upc_target = _norm((retraction_fields or {}).get("upc"))
    ref_target = _norm((retraction_fields or {}).get("ref_id"))
    if not upc_target and not ref_target:
        return None

    matches = []
    for row_num, row in enumerate(values[1:], start=2):
        if not any(_norm(c) for c in row):
            continue
        row_upc = _cell(row, idx.get("UPC"))
        row_ref = _cell(row, idx.get(SPOTIFY_REF_HEADER))
        if ref_target and row_ref == ref_target:
            matched = True
            ref_match = True
        else:
            matched = bool(upc_target and row_upc == upc_target)
            ref_match = False
        if not matched:
            continue
        status = _cell(row, idx.get("Status"))
        retracted = _cell(row, idx.get(RETRACTED_HEADER))
        matches.append({
            "row_num": row_num,
            "upc": row_upc,
            "title": _cell(row, idx.get("Title")),
            "artist": _cell(row, idx.get("Artist")),
            "status": status,
            "retracted": retracted,
            "ref_code": row_ref,
            "ref_match": ref_match,
            "unresolved": not _is_resolved_status(status),
            "not_retracted": not _is_retracted_value(retracted),
        })
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            1 if item["ref_match"] else 0,
            1 if item["unresolved"] else 0,
            1 if item["not_retracted"] else 0,
            item["row_num"],
        ),
    )



def build_retraction_summary_card(entries):
    count = len(entries)
    noun = "claim" if count == 1 else "claims"
    verb = "was" if count == 1 else "were"
    elements = [
        {
            "tag": "markdown",
            "content": f"Good news — Spotify confirmed **{count} {noun}** {verb} retracted in the latest {ACTIVE_REGION} scan.",
        },
        {"tag": "hr"},
    ]
    for entry in entries:
        title = entry.get("title") or "Unknown title"
        artist = entry.get("artist") or "Unknown artist"
        upc = entry.get("upc") or "N/A"
        retracted_at = entry.get("retracted_at") or "Unknown date"
        elements.append({
            "tag": "markdown",
            "content": f"• **{title}** — {artist}\nUPC: `{upc}`\nDate retracted: {retracted_at}",
        })
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": f"Tracker updates applied automatically in the {ACTIVE_REGION} sheet."})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": f"✅ {ACTIVE_REGION}: {count} claim retraction{'s' if count != 1 else ''} detected"},
        },
        "elements": elements,
    }



def run_retraction_pass(values, *, since_timestamp=None, full_history=False, notify=False):
    section_title = "PART 1A.2 — Backfill claim retractions" if full_history else "PART 1A.2 — Detect retracted claims"
    section(section_title)
    if not values or len(values) < 2:
        log("  No tracker rows available; skipping retraction processing.")
        return {
            "searched": 0,
            "candidates": 0,
            "matched": 0,
            "updated": 0,
            "unmatched": 0,
            "already_processed": 0,
            "already_marked": 0,
            "update_failures": 0,
            "card_posted": False,
        }

    headers = [_norm(h) for h in values[0]]
    idx = _row_lookup(headers)
    status_i = idx.get("Status")
    retracted_i = idx.get(RETRACTED_HEADER)
    if status_i is None or retracted_i is None:
        log("  ✗ Tracker headers missing Status or Retracted column; cannot process retractions.")
        return {
            "searched": 0,
            "candidates": 0,
            "matched": 0,
            "updated": 0,
            "unmatched": 0,
            "already_processed": 0,
            "already_marked": 0,
            "update_failures": 0,
            "card_posted": False,
        }

    search_kwargs = {"sender": RETRACTION_SENDER}
    if since_timestamp and not full_history:
        search_kwargs["start_time"] = since_timestamp
    if not full_history:
        search_kwargs["limit"] = 200

    try:
        candidates = search_inbox_messages("retracted", **search_kwargs)
    except Exception as exc:
        log(f"  ✗ Retraction mailbox search failed: {exc!r}")
        return {
            "searched": 0,
            "candidates": 0,
            "matched": 0,
            "updated": 0,
            "unmatched": 0,
            "already_processed": 0,
            "already_marked": 0,
            "update_failures": 1,
            "card_posted": False,
        }

    summary = {
        "searched": len(candidates),
        "candidates": 0,
        "matched": 0,
        "updated": 0,
        "unmatched": 0,
        "already_processed": 0,
        "already_marked": 0,
        "update_failures": 0,
        "card_posted": False,
    }
    notifications = []

    for candidate in candidates:
        message_id = _norm(candidate.get("message_id"))
        if not message_id:
            continue
        if is_retraction_already_processed(message_id):
            summary["already_processed"] += 1
            continue

        subject = candidate.get("subject", "")
        try:
            body, meta = fetch_email(message_id)
        except Exception as exc:
            summary["update_failures"] += 1
            log(f"  ⚠ Could not fetch retraction email {message_id}: {exc!r}")
            continue

        if not is_retraction_email(body, subject, meta):
            continue

        summary["candidates"] += 1
        retraction = extract_retraction_fields(body, subject, meta)
        match = match_tracker_row_for_retraction(values, retraction)
        state_record = {
            "region": ACTIVE_REGION,
            "message_id": message_id,
            "upc": _norm(retraction.get("upc")),
            "title": _norm(retraction.get("title")),
            "ref_code": _norm(retraction.get("ref_id")),
            "retracted_at": _norm(retraction.get("retracted_at") or candidate.get("date")),
            "matched": False,
        }

        if not match:
            summary["unmatched"] += 1
            log(f"  ↷ Retraction email {message_id} UPC {_norm(retraction.get('upc')) or 'N/A'} not found in tracker; skipping row creation.")
            _save_retracted_claim(message_id, state_record)
            continue

        summary["matched"] += 1
        row_num = match["row_num"]
        state_record.update({"matched": True, "tracker_row": row_num})
        wrote_ok = True
        changed = False

        if not _is_retracted_value(match["retracted"]):
            wrote_ok = write_cell(_col_letter(retracted_i), row_num, "Yes") and wrote_ok
            changed = True
        if not _is_resolved_status(match["status"]):
            wrote_ok = write_cell(_col_letter(status_i), row_num, STATUS_RESOLVED) and wrote_ok
            changed = True

        if not wrote_ok:
            summary["update_failures"] += 1
            log(f"  ⚠ Retraction update failed for tracker row {row_num}; leaving email uncheckpointed for retry.")
            continue

        if changed:
            summary["updated"] += 1
        else:
            summary["already_marked"] += 1

        _set_row_value(values, row_num, retracted_i, "Yes")
        _set_row_value(values, row_num, status_i, STATUS_RESOLVED)
        _save_retracted_claim(message_id, state_record)

        if notify:
            notifications.append({
                "title": match.get("title") or _norm(retraction.get("title")) or "Unknown title",
                "artist": match.get("artist") or "Unknown artist",
                "upc": match.get("upc") or _norm(retraction.get("upc")) or "N/A",
                "retracted_at": _format_retraction_date(retraction.get("retracted_at") or candidate.get("date")),
            })

    if notify and notifications:
        ok, _ = post_card(
            build_retraction_summary_card(notifications),
            chat_id=TARGET_CHAT_ID,
            expected_region=ACTIVE_REGION,
            context=f"{ACTIVE_REGION} retraction summary",
        )
        summary["card_posted"] = bool(ok)
        if ok:
            log(f"  ✓ Posted batched retraction summary card with {len(notifications)} entries.")
        else:
            log(f"  ⚠ Failed to post retraction summary card with {len(notifications)} entries.")
    elif notify:
        log("  No new matched retractions in the scan window; no group card posted.")

    log(f"  Retraction summary: {json.dumps(summary, ensure_ascii=False)}")
    return summary


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
    retracted_i = idx.get(RETRACTED_HEADER)

    missing = []
    for r, row in enumerate(values[1:], start=2):
        # ignore fully empty trailing rows
        if not any(_norm(c) for c in row):
            continue
        if _is_retracted_value(_cell(row, retracted_i)):
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
    retracted_i = idx.get(RETRACTED_HEADER)

    need_takedown = []
    need_assert = []
    for r, row in enumerate(values[1:], start=2):
        if not any(_norm(c) for c in row):
            continue
        if _is_retracted_value(_cell(row, retracted_i)):
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

    Loads runtime fallback files first, then canonical files, so canonical files
    take precedence when the same group-card message_id appears in both places.
    """
    out = {}
    for path in [*POSTED_CLAIMS_FALLBACK_FILES, *POSTED_CLAIMS_FILES]:
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
        def merge(current):
            if not isinstance(current, dict):
                current = {}
            current.update(dict(state or {}))
            return current

        update_json_state(ROOT / SPOTIFY_DM_STATE_FILE, merge, default=dict, ensure_ascii=False, indent=2)
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
        "claimant_category": _header_index(headers, "Claimant Category"),
        "admin_action": _header_index(headers, ADMIN_ACTION_HEADER),
        "retracted": _header_index(headers, RETRACTED_HEADER),
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
        retracted = _cell(row, idx["retracted"])
        if not _is_open_for_ops(status, admin_action, retracted):
            log(f"  • Skipping row {row_num} ({_cell(row, idx['upc']) or 'N/A'}): status={status!r}, admin_action={admin_action!r}, retracted={retracted!r}")
            continue
        if _cell(row, idx["email_status"]):  # already replied
            continue
        if _cell(row, idx["claimant_category"]) == "Internal self-claim":
            log(f"  • Skipping row {row_num} ({_cell(row, idx['upc']) or 'N/A'}): Tier 4 internal self-claim; no external reply DM.")
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
        def mutate(posted):
            if not isinstance(posted, dict):
                posted = {}
            for payload in posted.values():
                if isinstance(payload, dict) and payload.get("message_id") == old_message_id:
                    payload["message_id"] = new_message_id
            return posted

        update_json_state(POSTED_CLAIMS_FILE, mutate, default=dict, ensure_ascii=False, indent=2)
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
        "retracted": _header_index(headers, RETRACTED_HEADER),
    }
    today = date.today()
    refreshed = 0
    attempted = 0
    for row_num, row in enumerate(values[1:], start=2):
        status = _cell(row, idx["status"])
        admin_action = _cell(row, idx["admin_action"])
        retracted = _cell(row, idx["retracted"])
        if not _is_open_for_ops(status, admin_action, retracted):
            continue
        if _cell(row, idx["email_status"]):  # already handled
            continue
        card_msg_id = _cell(row, idx["card_msg"]) or _cell(row, idx["lark_msg"])
        if not card_msg_id:
            continue
        detected = parse_detected_date(_cell(row, idx["date"]))
        if detected is None:
            continue
        card_age_days = (today - detected).days
        if card_age_days >= MAX_LARK_CARD_PATCH_AGE_DAYS:
            log(
                f"  ℹ Skipping row {row_num}: card {card_msg_id} is {card_age_days} day(s) old; "
                "Lark no longer allows PATCH after 14 days."
            )
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
            log(
                f"  ⚠ Countdown refresh skipped for row {row_num}: "
                f"PATCH failed for {card_msg_id}; no replacement card will be posted."
            )
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
    previous_checkpoint_state = load_checkpoint_state()
    retraction_window_start = previous_checkpoint_state.get("updated_at")

    # A) Incremental scan
    try:
        results["scan"] = run_scan()
    except Exception as e:
        log(f"  ✗ Scan section error: {e!r}")
        results["scan"] = {"error": repr(e)}

    # Sheet sections — ensure required columns first so later passes read accurate headers.
    values = read_sheet_values("A:AF")
    try:
        admin_idx, admin_created = ensure_admin_action_column(values)
        results["admin_column"] = {"index": admin_idx, "created": admin_created}
    except Exception as e:
        log(f"  ✗ Admin column section error: {e!r}")
        results["admin_column"] = {"error": repr(e)}
        admin_idx = None
        admin_created = False

    try:
        retracted_idx, retracted_created = ensure_retracted_column(values)
        results["retracted_column"] = {"index": retracted_idx, "created": retracted_created}
    except Exception as e:
        log(f"  ✗ Retracted column section error: {e!r}")
        results["retracted_column"] = {"error": repr(e)}
        retracted_created = False

    # Ensure the Spotify reply columns exist before retraction / E/F reads.
    spotify_columns_ok = True
    try:
        ensure_spotify_columns(values)
    except Exception as e:
        spotify_columns_ok = False
        log(f"  ✗ Spotify column section error: {e!r}")

    try:
        major_label_indices, major_label_created = ensure_major_label_columns(values)
        results["major_label_columns"] = {"indices": major_label_indices, "created": major_label_created}
    except Exception as e:
        major_label_created = []
        log(f"  ✗ Major label column section error: {e!r}")
        results["major_label_columns"] = {"error": repr(e)}

    if admin_created or retracted_created or spotify_columns_ok or major_label_created:
        values = read_sheet_values("A:AF")

    # A.2) Retraction detection runs after the normal new-claim pass and before
    # later metrics/DM flows so resolved retractions disappear from open counts.
    try:
        results["retractions"] = run_retraction_pass(
            values,
            since_timestamp=retraction_window_start,
            notify=True,
        )
        if results["retractions"].get("updated"):
            values = read_sheet_values("A:AF")
    except Exception as e:
        log(f"  ✗ Retraction section error: {e!r}")
        results["retractions"] = {"error": repr(e)}

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

    # G) Spotify metadata/misrepresentation notices — daily re-send of the DM for
    # any notice still not Actioned. Region-agnostic and idempotent per day, so it
    # is safe to run from every region's workflow (only the first run of the day
    # re-sends a given notice).
    try:
        section("PART G — Spotify metadata notices (daily re-send)")
        results["metadata_notices"] = metadata_notice.resend_unresolved_notices()
    except Exception as e:
        log(f"  ✗ Metadata-notice section error: {e!r}")
        results["metadata_notices"] = {"error": repr(e)}

    # H) ACR takedown stream tracker — for ACR-tab cases marked Takedown on the
    # dashboard, refresh the original track's Aeolus streams and write the
    # since-takedown delta back to the "ACR Takedown Stream Tracker" sheet.
    # The tracker sheet is region-agnostic (not one of the 3 regional trackers),
    # and daily_workflow.py runs once per region per day, so this fires on
    # every regional invocation — but it's harmless: each row is throttled to
    # ~weekly internally via last_checked, so once the first region's run of
    # the day refreshes the rows that are due, the other regions' runs the
    # same day find nothing due and return immediately.
    try:
        section("PART H — ACR takedown stream tracker")
        results["takedown_stream_tracker"] = takedown_stream_tracker.run_daily_refresh()
    except Exception as e:
        log(f"  ✗ Takedown-stream-tracker section error: {e!r}")
        results["takedown_stream_tracker"] = {"error": repr(e)}

    # I) Claim event log — backfill card_posted times (Lark message create_time)
    # and stamp admin_action_seen for ops-handling-time metrics. Region-specific:
    # each regional run syncs its own tracker. Never raises.
    try:
        section("PART I — Claim event log sync")
        results["claim_event_log"] = claim_events.run_daily_sync_safe(ACTIVE_REGION)
    except Exception as e:
        log(f"  ✗ Claim-event-log section error: {e!r}")
        results["claim_event_log"] = {"error": repr(e)}

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
