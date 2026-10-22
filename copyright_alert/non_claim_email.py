#!/usr/bin/env python3
"""
copyright_alert/non_claim_email.py

Spotify sends several kinds of email from the SAME address as real infringement
claims (infringement-claim-response@spotify.com). Some of them merely MENTION
UPCs/claims that already exist — treating them as new claims posts a bogus card
and tracker row. This module recognises those and says WHY, so every claim-parsing
entry point can skip them.

Recognised (all verified against real emails, Oct 2026):
  * PPP / Excessive Infringement Policy reports — a monthly "your catalog is At
    Risk" mail with a table of already-known claims.
  * "Query regarding claim NNN" replies — Spotify answering our own reinstatement
    request, quoting it (with its UPC) below the fold.
  * Contest-instruction auto-replies — Spotify answering a message we sent to the
    wrong place, quoting the claim's takedown-notification subject and the
    affected URI/UPC so we know which thread to reply to.

A genuine claim body always says "We have received a complaint that your content
infringes ..." — that phrase wins over every rule below, so a real claim can never
be dropped by a keyword that happens to appear in it.
"""
from __future__ import annotations

import re

_REAL_CLAIM = re.compile(r"we have received a complaint that your content infring", re.IGNORECASE)

_PPP_REPORT = re.compile(r"excessive\s+infringement", re.IGNORECASE)
_PPP_CONTEXT = re.compile(r"policy|threshold|at\s+risk|monthly\s+infringement", re.IGNORECASE)

_CONTEST_REPLY = re.compile(
    r"if\s+you\s+would\s+like\s+to\s+contest\s+a\s+claim,?\s+please\s+respond\s+directly\s+to\s+the\s+"
    r"takedown\s+notification",
    re.IGNORECASE,
)

# Spotify answering OUR reinstatement/contest mail: "Please refer to our email sent
# to you with the subject line ..." — quotes our own message (with its UPC), so the
# UPC in it is not a new claim. Subject is "Query regarding claim NNN - ref:...".
_QUERY_REPLY = re.compile(r"please\s+refer\s+to\s+our\s+email\s+sent\s+to\s+you\s+with\s+the\s+subject\s+line", re.IGNORECASE)
_QUERY_SUBJECT = re.compile(r"^\s*query\s+regarding\s+claim\b", re.IGNORECASE)


def _flat(*parts) -> str:
    return re.sub(r"\s+", " ", " ".join(str(p or "") for p in parts))


def non_claim_reason(body, subject="", meta=None):
    """Return a short reason string if this email is NOT a new infringement claim,
    else None."""
    text = _flat(subject, body)
    if _REAL_CLAIM.search(text):
        return None
    if _PPP_REPORT.search(text) and _PPP_CONTEXT.search(text):
        return "PPP / Excessive Infringement policy report (existing claims, not new)"
    if _CONTEST_REPLY.search(text):
        return "Spotify contest-instruction reply (points to an existing takedown notice)"
    if _QUERY_REPLY.search(text) or _QUERY_SUBJECT.match(str(subject or "")):
        return "Spotify 'Query regarding claim' reply (answers our own reinstatement/contest mail)"
    return None


def is_non_claim_email(body, subject="", meta=None) -> bool:
    return non_claim_reason(body, subject, meta) is not None
