#!/usr/bin/env python3
"""
copyright_alert/spotify_reply.py

Spotify 5-business-day reply workflow — prepares the operator-facing reply
metadata for the callback daemon.

This module no longer sends the operator-facing DM card itself. Instead it
returns structured result data so `persistent_callback.py` can send the
product-approved "Draft ready" outcome card.

Lark Mail draft creation is delegated to
`copyright_alert.lark_mail_draft.create_reply_draft()`. The operator opens the
real draft and clicks Send manually in Lark Mail.

Three reply types are supported:

  - reply_agree(...)         -> we accept the claim is legitimate
  - reply_investigating(...) -> we are still investigating with the rights holder
  - reply_dispute(...)       -> we formally dispute the claim

Each function returns a structured dict:

    {
        "ok": bool,
        "mode": "draft" | "failed",
        "action": str,           # 'agree' | 'investigating' | 'dispute'
        "command": [str],        # logical draft helper call metadata
        "draft_id": str,         # populated on success
        "send_preview_url": str, # Lark Mail draft URL (review & send)
        "error": str | None,
    }

The bot's mailbox is soundon-copyright@bytedance.com.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# ── Make the copyright_alert package importable & anchor relative paths ───────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from copyright_alert.run_alert import MAILBOX  # noqa: E402
except Exception:  # pragma: no cover - defensive fallback
    MAILBOX = "soundon-copyright@bytedance.com"

from copyright_alert.lark_mail_draft import (  # noqa: E402
    LarkMailDraftError,
    create_reply_draft,
)

LOG_DIR = ROOT / "copyright_alert" / "logs"
LOG_FILE = LOG_DIR / "spotify_reply.log"

# ── Pre-made templates (professional English tone) ───────────────────────────
TEMPLATE_AGREE = (
    "Thank you for reaching out. We have reviewed the claim and agree with its "
    "legitimacy. We will proceed with the appropriate action on our end. "
    "Please let us know if you need anything further."
)

TEMPLATE_INVESTIGATING = (
    "Thank you for your message. We have received the claim and are currently "
    "reviewing the content and gathering information. We will provide an update "
    "as soon as possible. Thank you for your patience."
)

TEMPLATE_DISPUTE = (
    "Thank you for your message. We have reviewed the claim and respectfully "
    "dispute it, as we believe it is not legitimate. We are confident in the "
    "rights of our artist/distributor for this content, and we are prepared to "
    "provide all supporting documentation necessary to prove ownership if "
    "required. Please let us know how to proceed."
)


# ── Logging ──────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # pragma: no cover
        print(f"  (log write failed: {exc!r})", flush=True)


# ── HTML body builders ───────────────────────────────────────────────────────
def _wrap_html(paragraphs) -> str:
    blocks = "".join(f"<p>{p}</p>" for p in paragraphs if p is not None and str(p).strip())
    return blocks or "<p></p>"


def _context_line(upc: str, title: str) -> str:
    upc = (upc or "N/A").strip() or "N/A"
    title = (title or "N/A").strip() or "N/A"
    return f"<b>Release:</b> {title} &nbsp;|&nbsp; <b>UPC:</b> {upc}"


def _ref_line(ref_id: str) -> str:
    """Spotify's Salesforce thread tracker — the ref code MUST be preserved
    verbatim in the reply body so the reply is linked back to the case."""
    ref_id = (ref_id or "").strip()
    if not ref_id:
        return ""
    return f"{ref_id}"


def _draft_url_from_id(draft_id: str) -> str:
    if not draft_id:
        return ""
    return (
        "https://www.larkoffice.com/mail?draftId="
        f"{draft_id}&scene=send-preview&mailbox=soundon-copyright%40bytedance.com"
    )


# ── Backwards-compatible auth refresh hook ───────────────────────────────────
def _refresh_aime_credentials() -> int:
    """Legacy no-op kept for compatibility with callers that still invoke it.

    Draft creation now uses the persisted Lark Mail OAuth refresh token via
    copyright_alert.lark_mail_draft, so the old AIME JWT refresh path is no
    longer required for spotify reply draft creation.
    """
    _log("ℹ _refresh_aime_credentials(): no-op; spotify reply drafts now use Lark Mail OAuth.")
    return 0


# ── Core: create a threaded reply draft ──────────────────────────────────────
def _send_reply(source_email_message_id: str, body_html: str, action: str,
                claimant_email: str = "", fallback_subject: str = "",
                cc: str = "", note: str = "", upc: str = "",
                ref_id: str = "") -> Dict[str, Any]:
    """Create a threaded reply draft via OAuth-backed mail draft helper.

    `claimant_email` and `fallback_subject` are retained for backwards
    compatibility. The helper still receives them so the caller-visible
    signatures and command metadata stay stable, but the reply remains anchored
    to the original message thread.

    `cc` (optional extra recipient) and `note` (optional text appended to the
    email body) are the operator-supplied optional fields. Both are ignored when
    blank, so the normal flow is unchanged.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "mode": "failed",
        "action": action,
        "command": [],
        "draft_id": "",
        "send_preview_url": "",
        "message_id": "",
        "mail_deep_link": "",
        "draft_text": "",
        "error": None,
    }

    cc = (cc or "").strip()
    note = (note or "").strip()
    source_email_message_id = (source_email_message_id or "").strip()
    upc = str(upc or "").strip()
    ref_id = str(ref_id or "").strip()

    # We can thread the reply either from the source email message_id carried by
    # the card, or by locating the original claim email via its UPC / ref code.
    if not source_email_message_id and not (upc or ref_id):
        result["error"] = (
            "Missing source email message ID for this UPC — cannot create "
            "threaded reply draft (Spotify needs the In-Reply-To header from "
            "the original claim email to link the reply back to the case)."
        )
        _log(f"✗ {action}: missing source_email_message_id and no upc/ref_id fallback.")
        return result

    # Append the optional operator note to the body before draft creation.
    if note:
        body_html = f"{body_html}\n<p>{note}</p>"

    cmd = [
        "create_reply_draft",
        f"mailbox={MAILBOX}",
        f"thread_message_id={source_email_message_id or '<lookup-by-upc/ref>'}",
        f"to={claimant_email or '<thread-derived>'}",
        f"subject={fallback_subject or '<thread-derived>'}",
    ]
    if cc:
        cmd.append(f"cc={cc}")
    if note:
        cmd.append("note=<appended>")
    result["command"] = cmd
    _log(
        f"→ {action}: create_reply_draft to thread {source_email_message_id or '(lookup)'} "
        f"(mailbox={MAILBOX}, cc={cc or 'none'}, note={'yes' if note else 'no'})"
    )

    try:
        payload = create_reply_draft(
            mailbox=MAILBOX,
            thread_message_id=source_email_message_id,
            to=claimant_email or MAILBOX,
            subject=fallback_subject or f"Spotify infringement claim response - {action}",
            body_html=body_html,
            cc=cc or None,
            upc=upc,
            ref_id=ref_id,
        )
        draft_id = str(payload.get("draft_id") or "").strip()
        draft_url = str(payload.get("draft_link") or "").strip()
        if not draft_url and draft_id:
            draft_url = _draft_url_from_id(draft_id)
        if not draft_id:
            raise LarkMailDraftError(
                "create_reply_draft response missing draft_id: "
                f"{json.dumps(payload, ensure_ascii=False)[:800]}"
            )
        result["ok"] = True
        result["mode"] = "draft"
        result["draft_id"] = draft_id
        result["send_preview_url"] = draft_url
        result["mail_deep_link"] = draft_url
        result["draft_text"] = body_html
        _log(
            f"✓ {action}: DRAFT created "
            f"(draft_id={result['draft_id'] or 'N/A'}, "
            f"send_preview_url={result['send_preview_url'] or 'N/A'})."
        )
        return result
    except Exception as exc:
        result["error"] = f"reply draft creation failed: {exc!r}"
        _log(f"✗ {action}: {result['error']}")
        return result


# ── Public reply functions ───────────────────────────────────────────────────
def reply_agree(source_email_message_id: str, claimant_email: str,
                upc: str, title: str, ref_id: str = "",
                cc: str = "", note: str = "") -> Dict[str, Any]:
    body = _wrap_html([TEMPLATE_AGREE, _context_line(upc, title), _ref_line(ref_id)])
    return _send_reply(source_email_message_id, body, "agree", claimant_email,
                       f"Spotify infringement claim response - UPC {upc}",
                       cc=cc, note=note, upc=upc, ref_id=ref_id)


def reply_investigating(source_email_message_id: str, claimant_email: str,
                        upc: str, title: str, ref_id: str = "",
                        cc: str = "", note: str = "") -> Dict[str, Any]:
    body = _wrap_html([TEMPLATE_INVESTIGATING, _context_line(upc, title), _ref_line(ref_id)])
    return _send_reply(source_email_message_id, body, "investigating", claimant_email,
                       f"Spotify infringement claim response - UPC {upc}",
                       cc=cc, note=note, upc=upc, ref_id=ref_id)


def reply_dispute(source_email_message_id: str, claimant_email: str, upc: str,
                  title: str, custom_message: str = "", ref_id: str = "",
                  cc: str = "", note: str = "") -> Dict[str, Any]:
    # custom_message accepted for backwards-compat; not used.
    body = _wrap_html([TEMPLATE_DISPUTE, _context_line(upc, title), _ref_line(ref_id)])
    return _send_reply(source_email_message_id, body, "dispute", claimant_email,
                       f"Spotify infringement claim response - UPC {upc}",
                       cc=cc, note=note, upc=upc, ref_id=ref_id)


def send_reply(reply_type: str, source_email_message_id: str, claimant_email: str,
               upc: str, title: str, custom_message: str = "",
               ref_id: str = "", cc: str = "", note: str = "") -> Dict[str, Any]:
    """Dispatch helper used by the card-action callback."""
    reply_type = (reply_type or "").strip().lower()
    if reply_type == "agree":
        return reply_agree(source_email_message_id, claimant_email, upc, title, ref_id,
                           cc=cc, note=note)
    if reply_type == "investigating":
        return reply_investigating(source_email_message_id, claimant_email, upc, title, ref_id,
                                   cc=cc, note=note)
    if reply_type == "dispute":
        return reply_dispute(source_email_message_id, claimant_email, upc, title,
                             custom_message, ref_id, cc=cc, note=note)
    _log(f"✗ unknown reply_type: {reply_type!r}")
    return {
        "ok": False,
        "mode": "failed",
        "action": reply_type or "unknown",
        "command": [],
        "draft_id": "",
        "send_preview_url": "",
        "error": f"Unknown reply type {reply_type!r} — expected 'agree', 'investigating', or 'dispute'.",
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        args = json.loads(sys.argv[1])
        print(json.dumps(send_reply(**args), ensure_ascii=False, indent=2))
    else:
        print("Usage: spotify_reply.py '{\"reply_type\":\"agree\",\"source_email_message_id\":\"...\",\"claimant_email\":\"...\",\"upc\":\"...\",\"title\":\"...\"}'")
