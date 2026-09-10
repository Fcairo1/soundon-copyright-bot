#!/usr/bin/env python3
"""DM UPC lookup: search the copyright inbox by UPC and format a plain-text reply.

Triggered when a user DMs the bot a numeric UPC (12-13 digits). Reuses the
inbox triage + email parsing logic from `run_alert.py`.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import List, Tuple

from copyright_alert import run_alert as ra


UPC_PATTERN = re.compile(r"^\d{12,13}$")
TRIAGE_EXACT_MAX = 100
TRIAGE_FALLBACK_MAX = 400
FALLBACK_CLAIM_QUERIES = (
    ra.TRIAGE_QUERY,
    "Possibly Infringing",
    "Takedown Notification",
    "Notification Warning",
)


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
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        print(f"dm_upc_lookup triage failed query={query!r} rc={res.returncode}: {(res.stdout + res.stderr)[:500]}")
        return []
    parsed = ra.parse_lark_json(res.stdout)
    if not parsed:
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
    """Collect likely claim emails for a UPC, with broad fallback queries.

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
    if ordered_hits:
        return ordered_hits

    for query in FALLBACK_CLAIM_QUERIES:
        _extend(_triage_search(query, max_results=TRIAGE_FALLBACK_MAX))

    return ordered_hits


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


def lookup_upc(upc: str) -> str:
    """Search inbox for the UPC and return a formatted plain-text reply."""
    upc = upc.strip()
    if not is_upc(upc):
        return f"'{upc}' doesn't look like a UPC (expecting 12-13 digits)."

    messages = _candidate_messages(upc)
    matches: List[Tuple[dict, str, str]] = []  # (extracted_fields, subject, date)
    seen_match_keys = set()
    for m in messages:
        msg_id = m.get("message_id")
        subject = m.get("subject", "")
        date = m.get("date", "")
        if not msg_id:
            continue
        try:
            body, meta = ra.fetch_email(msg_id)
        except Exception as exc:
            print(f"dm_upc_lookup fetch_email failed for {msg_id}: {exc!r}")
            continue
        # Confirm UPC actually appears in subject or body to avoid false hits.
        haystack = f"{subject}\n{body}"
        if not _contains_upc(haystack, upc):
            continue
        try:
            ef = ra.extract_fields(body, subject, meta)
        except Exception as exc:
            print(f"dm_upc_lookup extract_fields failed: {exc!r}")
            continue
        # Prefer exact extracted UPC matches when available, but tolerate
        # formatting noise in the raw body/subject (spaces, line wraps, etc.).
        extracted_upc = _digit_only((ef.get("upc") or "").strip())
        if extracted_upc and extracted_upc != upc:
            # The UPC string was just incidentally present (e.g. forwarded thread).
            continue
        match_key = msg_id or f"{subject}|{date}"
        if match_key in seen_match_keys:
            continue
        seen_match_keys.add(match_key)
        matches.append((ef, subject, date or meta.get("date_formatted", "")))

    if not matches:
        return f"No infringement claims found for UPC {upc}."

    # Title: prefer first non-N/A title from emails, fall back to Aeolus.
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
        track_title = _aeolus_title_for_upc(upc)

    header_lines = [
        f"🎵 UPC: {upc}",
        f"🎼 Track / Release: {track_title}",
        f"🔎 Found {len(matches)} matching claim email(s).",
        "",
    ]
    body_lines: List[str] = []
    for i, (ef, subject, date) in enumerate(matches, start=1):
        body_lines.extend(_format_match(i, len(matches), ef, subject, date))
        body_lines.append("")

    return "\n".join(header_lines + body_lines).rstrip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m copyright_alert.dm_upc_lookup <upc>")
        sys.exit(1)
    print(lookup_upc(sys.argv[1]))
