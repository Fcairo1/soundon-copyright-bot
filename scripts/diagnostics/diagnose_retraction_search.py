#!/usr/bin/env python3
"""Why does the retraction search find 0 messages? Read-only matrix of search variants.

Usage (from the repo root, with the AIME env loaded):
    python3 scripts/diagnostics/diagnose_retraction_search.py

For every variant prints the number of hits and up to 3 (date, sender, subject) rows, or
the first line of the error. Subjects/senders only: no bodies, no tokens, nothing written.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import lark_auth  # noqa: E402
from copyright_alert import run_alert as ra  # noqa: E402

SENDER = ra.RETRACTION_SENDER
QUERIES = tuple(ra.RETRACTION_SEARCH_QUERIES)
MAILBOXES = ("me", ra.MAILBOX)


def summarize(parsed: dict, limit: int = 3) -> tuple[int, list[str]]:
    """Return (hit count, sample lines) for either a `user_mailboxes search` payload
    (items[].meta_data) or a `+triage` payload (messages[])."""
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    items = parsed.get("items") or data.get("items")
    rows = []
    if items:
        for it in items:
            meta = it.get("meta_data") if isinstance(it.get("meta_data"), dict) else {}
            rows.append(f"{meta.get('create_time', '?')} | {meta.get('from', '?')} | {str(meta.get('title', ''))[:70]}")
        return len(items), rows[:limit]
    msgs = parsed.get("messages") or data.get("messages") or []
    for m in msgs:
        rows.append(f"{m.get('date', '?')} | {m.get('from', m.get('sender', '?'))} | {str(m.get('subject', ''))[:70]}")
    return len(msgs), rows[:limit]


def run(cmd: list[str]) -> str:
    res = lark_auth.run_lark_cli(cmd, context="diagnose_retraction_search", timeout=120)
    if res.returncode != 0:
        first = ((res.stderr or res.stdout).strip().splitlines() or ["unknown error"])[0][:160]
        return f"ERROR rc={res.returncode}: {first}"
    parsed = ra.parse_lark_json(res.stdout)
    if not parsed:
        return "unreadable output: " + (res.stdout or "")[:120].replace("\n", " ")
    count, rows = summarize(parsed)
    return f"{count} hit(s)" + "".join(f"\n        {r}" for r in rows)


def main() -> int:
    print(f"sender filter = {SENDER}\n")
    for mailbox in MAILBOXES:
        print(f"== user_mailboxes search, user_mailbox_id={mailbox!r}")
        variants = [("from-filter only (empty query)", "", {"from": [SENDER]})]
        for q in QUERIES:
            variants.append((f"query {q!r}, no filter", q, {}))
            variants.append((f"query {q!r} + from-filter", q, {"from": [SENDER]}))
        for label, query, filt in variants:
            cmd = ["lark-cli", "mail", "user_mailboxes", "search",
                   "--params", json.dumps({"user_mailbox_id": mailbox, "page_size": 5}),
                   "--data", json.dumps({"query": query, "filter": filt})]
            print(f"  - {label}: {run(cmd)}")
    print(f"\n== +triage --mailbox {ra.MAILBOX} (the call the daily scan uses)")
    for q in QUERIES + (SENDER,):
        cmd = ["lark-cli", "mail", "+triage", "--mailbox", ra.MAILBOX, "--query", q,
               "--max", "10", "--format", "json"]
        print(f"  - query {q!r}: {run(cmd)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
