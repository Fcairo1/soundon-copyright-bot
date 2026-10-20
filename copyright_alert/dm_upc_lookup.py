#!/usr/bin/env python3
"""DM UPC lookup: search the copyright inbox by UPC and format a plain-text reply.

Triggered when a user DMs the bot a numeric UPC (12-13 digits). Reuses the
inbox triage + email parsing logic from `run_alert.py`.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from typing import List, Optional, Tuple

from copyright_alert import lark_auth
from copyright_alert import run_alert as ra


UPC_PATTERN = re.compile(r"^\d{12,13}$")
TRIAGE_EXACT_MAX = 100
TRIAGE_FALLBACK_MAX = 100
# The broad sweep (no UPC in the query) is a last resort for a UPC that has no
# tracker row and no exact-search hit. Unbounded, it listed 4 x 400 emails and
# then fetched every one — measured ~37s per 400-message listing alone, so a
# plain "no claim" answer could take the better part of an hour. Keep it bounded.
FALLBACK_MAX_FETCH = 30
FALLBACK_TIME_BUDGET_SEC = 60
FALLBACK_CLAIM_QUERIES = (
    ra.TRIAGE_QUERY,
    "Possibly Infringing",
    "Takedown Notification",
    "Notification Warning",
)


TRACKER_REGIONS = ("BR", "US", "SPLA")

# Problems hit while answering ONE lookup (inbox search failed, token expired,
# a tracker was unreadable...). A failed search used to be indistinguishable
# from "no claims" — the bot told people "No infringement claims found" whenever
# anything went wrong. Thread-local because DM handlers can run concurrently.
_state = threading.local()


def _note_error(message: str) -> None:
    errors = getattr(_state, "errors", None)
    if errors is not None and message not in errors:
        errors.append(message)


def is_upc(text: str) -> bool:
    return bool(UPC_PATTERN.match((text or "").strip()))


def _triage_search(query: str, *, max_results: int) -> List[dict]:
    """Search inbox via lark-cli triage and return raw message hits."""
    cmd = [
        "lark-cli", "mail", "+triage",
        "--mailbox", ra.MAILBOX,
        "--query", query,
        "--max", str(max_results),
        "--format", "json",
    ]
    # The long-lived daemon's AIME JWT can expire between DMs.
    lark_auth.ensure_jwt_fresh("dm_upc_lookup mail triage")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as exc:  # timeout etc.
        print(f"dm_upc_lookup triage raised query={query!r}: {exc!r}")
        _note_error(f"inbox search failed ({type(exc).__name__})")
        return []
    if res.returncode != 0:
        print(f"dm_upc_lookup triage failed query={query!r} rc={res.returncode}: {(res.stdout + res.stderr)[:500]}")
        _note_error("inbox search failed: " + ((res.stderr or res.stdout).strip().splitlines() or ["unknown error"])[0][:120])
        return []
    parsed = ra.parse_lark_json(res.stdout)
    if not parsed:
        _note_error("inbox search returned an unreadable response")
        return []
    return parsed.get("messages") or (parsed.get("data") or {}).get("messages") or []


def _digit_only(text: str) -> str:
    return "".join(re.findall(r"\d+", text or ""))


def _contains_upc(text: str, upc: str) -> bool:
    haystack = text or ""
    if upc in haystack:
        return True
    return upc in _digit_only(haystack)


def _candidate_messages(upc: str) -> List[dict]:
    """Exact-UPC inbox search hits (the broad fallback sweep lives in
    ``_fallback_messages`` and only runs from ``lookup_upc``, bounded).

    Root-cause hardening:
    relying only on an exact ``mail +triage --query <upc>`` search can produce
    false negatives if Lark search misses that specific message body, if the hit
    falls outside a small result cap, or if a claim uses a non-standard subject.
    We first try the exact UPC query, then fall back to broader claim-oriented
    searches and do our own body/subject UPC verification.
    """
    ordered_hits: List[dict] = []
    seen_ids = set()

    def _extend(messages: List[dict]):
        for message in messages:
            msg_id = (message or {}).get("message_id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            ordered_hits.append(message)

    _extend(_triage_search(upc, max_results=TRIAGE_EXACT_MAX))
    return ordered_hits


def _fallback_messages(upc: str, deadline: Optional[float] = None) -> List[dict]:
    """Broad claim-oriented searches (no UPC in the query); callers verify the UPC.
    Newest first, stops listing once ``deadline`` (a time.monotonic() value) passes."""
    hits: List[dict] = []
    seen_ids = set()
    for query in FALLBACK_CLAIM_QUERIES:
        if deadline is not None and time.monotonic() > deadline:
            break
        for message in _triage_search(query, max_results=TRIAGE_FALLBACK_MAX):
            msg_id = (message or {}).get("message_id")
            if msg_id and msg_id not in seen_ids:
                seen_ids.add(msg_id)
                hits.append(message)
    return hits


def _aeolus_title_for_upc(upc: str) -> str:
    """Best-effort Aeolus lookup for the album/track title by UPC."""
    try:
        row = ra.query_aeolus(upc, id_type="upc")
        if isinstance(row, dict):
            for key in ("album_title", "song_name", "track_title", "title"):
                val = row.get(key)
                if val and val != "N/A":
                    return str(val)
    except Exception as exc:
        print(f"dm_upc_lookup aeolus lookup failed: {exc!r}")
    return "N/A"


def _format_match(idx: int, total: int, ef: dict, subject: str, date: str) -> List[str]:
    title = ef.get("title") or "N/A"
    if title == "N/A":
        title = ef.get("content") or "N/A"
    claimant = ef.get("claimant_name") or "N/A"
    company = ef.get("claimant_company") or "N/A"
    message = ef.get("claimant_message") or "no message"
    if len(message) > 800:
        message = message[:800] + "…"
    header = f"📨 Match {idx}/{total}" if total > 1 else "📨 Match"
    lines = [
        header,
        f"• Date: {date or 'N/A'}",
        f"• Subject: {subject or 'N/A'}",
        f"• Track / Release: {title}",
        f"• Claimant: {claimant}" + (f" ({company})" if company and company != 'N/A' else ""),
        f"• Email: {ef.get('claimant_email') or 'N/A'}",
        f"• Claim message:\n{message}",
    ]
    return lines


def _norm_upc(value: str) -> str:
    """Digits only, leading zeros dropped — Excel/DM copy-paste often strips them."""
    return _digit_only(value).lstrip("0")


def _entry_for_upc(body: str, subject: str, meta: dict, upc: str) -> dict:
    """Fields for the claim entry that matches ``upc``.

    One claim email can list many UPCs (e.g. a Spotify notice with 40 albums, or
    AudioSalad "UPC(s): a, b"). ``extract_fields`` only returns the FIRST one, so
    the old ``extracted_upc != upc -> skip`` rule dropped every other UPC in the
    email and answered "No infringement claims found" for real claims.
    """
    try:
        entries = ra.extract_claim_entries(body, subject, meta)
    except Exception as exc:
        print(f"dm_upc_lookup extract_claim_entries failed: {exc!r}")
        entries = [ra.extract_fields(body, subject, meta)]
    for entry in entries:
        if _norm_upc(entry.get("upc") or "") == _norm_upc(upc):
            return entry
    return entries[0]


def _collect_matches(messages, upc, matches, seen_match_keys, processed_ids,
                     *, max_fetch: Optional[int] = None, deadline: Optional[float] = None) -> None:
    fetched = 0
    for m in messages:
        if max_fetch is not None and fetched >= max_fetch:
            break
        if deadline is not None and time.monotonic() > deadline:
            break
        msg_id = m.get("message_id")
        subject = m.get("subject", "")
        date = m.get("date", "")
        if not msg_id or msg_id in processed_ids:
            continue
        processed_ids.add(msg_id)
        fetched += 1
        try:
            body, meta = ra.fetch_email(msg_id)
        except Exception as exc:
            print(f"dm_upc_lookup fetch_email failed for {msg_id}: {exc!r}")
            _note_error("couldn't open some matching emails")
            continue
        # The UPC must actually appear in the subject or body (search hits can be loose).
        if not _contains_upc(f"{subject}\n{body}", upc):
            continue
        try:
            ef = _entry_for_upc(body, subject, meta, upc)
        except Exception as exc:
            print(f"dm_upc_lookup extract_fields failed: {exc!r}")
            continue
        match_key = msg_id or f"{subject}|{date}"
        if match_key in seen_match_keys:
            continue
        seen_match_keys.add(match_key)
        matches.append((ef, subject, date or meta.get("date_formatted", "")))


def _tracker_lookup(upc: str) -> List[dict]:
    """Rows for this UPC in the BR/US/SPLA claim trackers (one row per UPC).

    The trackers are what the bot actually posted/ingested, and they're read via
    the persisted OAuth sheet API, so this still works when the inbox search
    can't (expired mail token, search miss, multi-UPC email).
    """
    from copyright_alert import handle_callback as hc

    wanted = _norm_upc(upc)
    hits: List[dict] = []
    for region in TRACKER_REGIONS:
        try:
            values = hc.read_sheet_values(region)
        except Exception as exc:
            print(f"dm_upc_lookup tracker read failed for {region}: {exc!r}")
            _note_error(f"{region} tracker unreadable")
            continue
        if not values:
            _note_error(f"{region} tracker unreadable")
            continue
        headers = [str(h or "").strip() for h in values[0]]
        idx = {h: i for i, h in enumerate(headers) if h}
        if "UPC" not in idx:
            _note_error(f"{region} tracker has no UPC column")
            continue

        def cell(row, name):
            i = idx.get(name)
            return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

        for row in values[1:]:
            if _norm_upc(cell(row, "UPC")) == wanted and wanted:
                hits.append({
                    "region": region,
                    "status": cell(row, "Status") or "(no status yet)",
                    "date": cell(row, "Date Received"),
                    "title": cell(row, "Title"),
                    "claimant": cell(row, "Claimant"),
                    "source": cell(row, "Email Source"),
                    "dsp": cell(row, "DSP"),
                })
    return hits


def _format_tracker_hits(hits: List[dict]) -> List[str]:
    lines = [f"📋 In the claims tracker ({len(hits)} row{'s' if len(hits) != 1 else ''}):"]
    for h in hits:
        bits = [h["region"], f"status: {h['status']}"]
        if h["date"]:
            bits.append(f"received {h['date']}")
        if h["claimant"] and h["claimant"] != "N/A":
            bits.append(f"claimant: {h['claimant']}")
        via = " / ".join(x for x in (h["source"], h["dsp"]) if x and x != "N/A")
        if via:
            bits.append(f"via {via}")
        lines.append("• " + " · ".join(bits))
    return lines


def lookup_upc(upc: str) -> str:
    """Check the inbox AND the trackers for the UPC and return a plain-text reply.

    Never answers "no claims" when a check actually failed — it says what it
    couldn't check instead.
    """
    upc = upc.strip()
    if not is_upc(upc):
        return f"'{upc}' doesn't look like a UPC (expecting 12-13 digits)."

    _state.errors = []
    matches: List[Tuple[dict, str, str]] = []  # (extracted_fields, subject, date)
    seen_match_keys = set()
    processed_ids = set()

    tracker_hits = _tracker_lookup(upc)
    _collect_matches(_candidate_messages(upc), upc, matches, seen_match_keys, processed_ids)
    if not matches and not tracker_hits and not _state.errors:
        # Nothing in the trackers and the exact search verified nothing (it can
        # return loose hits, or miss the message): sweep the newest broad claim
        # searches — bounded, see FALLBACK_* above.
        deadline = time.monotonic() + FALLBACK_TIME_BUDGET_SEC
        _collect_matches(_fallback_messages(upc, deadline), upc, matches, seen_match_keys, processed_ids,
                         max_fetch=FALLBACK_MAX_FETCH, deadline=deadline)
    errors = list(_state.errors)
    _state.errors = None

    warning = []
    if errors:
        warning = ["⚠️ Couldn't fully check: " + "; ".join(errors) + ".", ""]

    if not matches and not tracker_hits:
        if errors:
            return "\n".join([f"⚠️ I couldn't confirm whether UPC {upc} has a claim — " + "; ".join(errors)
                              + ". Please try again in a few minutes."])
        return (f"No infringement claims found for UPC {upc} "
                "(checked the claims inbox and the BR/US/SPLA trackers).")

    # Title: prefer first non-N/A title from emails, then the tracker, then Aeolus.
    track_title = "N/A"
    for ef, _, _ in matches:
        for key in ("title", "content"):
            val = ef.get(key)
            if val and val != "N/A":
                track_title = val
                break
        if track_title != "N/A":
            break
    if track_title == "N/A":
        track_title = next((h["title"] for h in tracker_hits if h["title"] and h["title"] != "N/A"), "N/A")
    if track_title == "N/A":
        track_title = _aeolus_title_for_upc(upc)

    header_lines = [f"🎵 UPC: {upc}", f"🎼 Track / Release: {track_title}"]
    if matches:
        header_lines.append(f"🔎 Found {len(matches)} matching claim email(s).")
    else:
        header_lines.append("🔎 A claim exists for this UPC (found in the tracker; the original email wasn't "
                            "retrievable live).")
    header_lines.append("")
    body_lines: List[str] = []
    for i, (ef, subject, date) in enumerate(matches, start=1):
        body_lines.extend(_format_match(i, len(matches), ef, subject, date))
        body_lines.append("")
    if tracker_hits:
        body_lines.extend(_format_tracker_hits(tracker_hits))
        body_lines.append("")

    return "\n".join(warning + header_lines + body_lines).rstrip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m copyright_alert.dm_upc_lookup <upc>")
        sys.exit(1)
    print(lookup_upc(sys.argv[1]))
