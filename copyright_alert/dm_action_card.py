#!/usr/bin/env python3
"""
copyright_alert/dm_action_card.py

Sends a private DM "action card" to filipe.cairo for each open Spotify claim,
one day after detection. The card lets the operator pick a reply with one click:

  ✅ Agree with claim
  🔍 Investigating – will follow up
  ⚖️ Dispute claim   (with a free-text input for the custom dispute message)

Each button carries a `spotify_reply` callback value that is handled by
persistent_callback.handle_card_action, which then calls spotify_reply.py and
writes the result to the tracker's "Email Status" column.

The card is sent with the copyright bot's identity (same token as the group
cards) so that button clicks route back to the bot's persistent callback daemon.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert.lark_auth import request_json_with_auth_retry  # noqa: E402
from copyright_alert.run_alert import _get_bot_access_token  # noqa: E402
# C1: unified 5-business-day BRT reply deadline (single source of truth).
from copyright_alert.tag_managers import (  # noqa: E402
    REPLY_DEADLINE_WORKDAYS as REPLY_DEADLINE_BUSINESS_DAYS,
    business_days_remaining_brt,
)

OPERATOR_EMAIL = "filipe.cairo@bytedance.com"
ACTION_FALLBACK_LABEL = "⏳ None selected yet"
ACTION_FIELD_CANDIDATES = (
    "manager_action",
    "selected_action",
    "action_requested",
    "action_request",
    "action_label",
    "reply_action",
    "reply_type_label",
    "reply_type",
    "admin_action_taken",
)
ACTION_SEARCH_FILES = (
    ROOT / "copyright_alert/posted_cards.json",
    ROOT / "copyright_alert/posted_claims.json",
)


# ── Date / countdown helpers ─────────────────────────────────────────────────
def parse_detected_date(value):
    """Parse the 'Detected At' / 'Date Received' value into a date.

    Accepts ISO date ('2026-06-19') or full ISO timestamp ('2026-06-12T21:14:16Z').
    Returns None if it cannot be parsed.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s or s == "N/A":
        return None
    s = s.replace("Z", "+00:00")
    # Try full datetime first, then plain date.
    for parser in (
        lambda x: datetime.fromisoformat(x).date(),
        lambda x: datetime.strptime(x[:10], "%Y-%m-%d").date(),
    ):
        try:
            return parser(s)
        except Exception:
            continue
    return None


def business_days_elapsed(start: date, end: date) -> int:
    """Number of weekdays (Mon-Fri) strictly after `start` up to and including `end`."""
    if not start or not end or end <= start:
        return 0
    days = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # 0-4 == Mon-Fri
            days += 1
    return days


def business_days_remaining(detected_at, today: date = None) -> int:
    # C1: delegate to the unified BRT business-day deadline so the DM card, the
    # group card, and the manager SLA always agree. The BRT "today" is used as
    # the reference; the legacy `today` argument is accepted for backward
    # compatibility but ignored (all callers rely on BRT).
    return business_days_remaining_brt(detected_at)


# ── open_id resolution ───────────────────────────────────────────────────────
def resolve_open_id(email: str) -> str:
    """Resolve a user's open_id from their email using the copyright bot token."""

    def make_request():
        token = _get_bot_access_token()
        if not token:
            raise RuntimeError("Could not get bot access token")
        url = "https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id"
        body = json.dumps({"emails": [email]}).encode("utf-8")
        return urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        )

    try:
        data = request_json_with_auth_retry(make_request, timeout=20, context=f"dm_action_card.resolve_open_id:{email}")
        for u in (data.get("data") or {}).get("user_list", []):
            if u.get("user_id"):
                return u["user_id"]
    except Exception as exc:
        print(f"  ⚠ resolve_open_id failed for {email}: {exc!r}", flush=True)
    return ""


# ── Card builder ─────────────────────────────────────────────────────────────
def _v(val) -> str:
    s = str(val).strip() if val is not None else ""
    return s if s and s != "N/A" else "N/A"


def _countdown_label(days_remaining: int) -> str:
    if days_remaining > 1:
        return f"🟢 {days_remaining} business days remaining"
    if days_remaining == 1:
        return "🟡 1 business day remaining"
    if days_remaining == 0:
        return "🟠 Due today"
    return f"🔴 Overdue by {abs(days_remaining)} business day(s)"


def _load_json_file(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _format_action_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized:
        return ""
    reply_type_map = {
        "agree": "✅ Agree with claim",
        "investigating": "🔍 Investigating – will follow up",
        "dispute": "⚖️ Dispute claim",
    }
    return reply_type_map.get(normalized.lower(), normalized)


def _extract_candidate_action(record: Any) -> str:
    if not isinstance(record, dict):
        return ""

    for key in ACTION_FIELD_CANDIDATES:
        formatted = _format_action_value(record.get(key))
        if formatted:
            return formatted

    for key, value in record.items():
        key_l = str(key).lower()
        if key_l == "action" or "action" not in key_l:
            continue
        formatted = _format_action_value(value)
        if formatted:
            return formatted

    return ""


def _find_action_for_upc(payload: Any, upc: str) -> str:
    if isinstance(payload, dict):
        if str(payload.get("upc", "")).strip() == upc:
            action_value = _extract_candidate_action(payload)
            if action_value:
                return action_value
        for value in payload.values():
            action_value = _find_action_for_upc(value, upc)
            if action_value:
                return action_value
    elif isinstance(payload, list):
        for item in payload:
            action_value = _find_action_for_upc(item, upc)
            if action_value:
                return action_value
    return ""


def get_action_requested_label(case: dict) -> str:
    upc = str(case.get("upc") or "").strip()
    if not upc or upc == "N/A":
        return ACTION_FALLBACK_LABEL

    for path in ACTION_SEARCH_FILES:
        action_value = _find_action_for_upc(_load_json_file(path), upc)
        if action_value:
            return action_value
    return ACTION_FALLBACK_LABEL


def build_dm_action_card(case: dict) -> dict:
    """Build the private DM action card for one Spotify claim case."""
    upc = _v(case.get("upc"))
    isrc = _v(case.get("isrc"))
    title = _v(case.get("title"))
    artist = _v(case.get("artist"))
    claimant_name = _v(case.get("claimant_name"))
    claimant_email = _v(case.get("claimant_email"))
    dsp = _v(case.get("dsp", "Unknown"))
    detected_at = _v(case.get("detected_at"))
    days_remaining = business_days_remaining(case.get("detected_at"))
    action_requested = _v(get_action_requested_label(case))

    # Shared value payload carried by every button.
    # Keep both source_email_message_id and the legacy source_message_id alias
    # so manually-built / older helper payloads remain clickable.
    source_email_message_id = (
        case.get("source_email_message_id")
        or case.get("source_message_id")
        or ""
    )
    base_value = {
        "action": "spotify_reply",
        "upc": case.get("upc", "N/A"),
        "isrc": case.get("isrc", "N/A"),
        "source_email_message_id": source_email_message_id,
        "source_message_id": source_email_message_id,
        "claimant_email": case.get("claimant_email", "N/A"),
        "title": case.get("title", "N/A"),
        "artist": case.get("artist", "N/A"),
        "lark_card_message_id": case.get("lark_card_message_id", ""),
        "tracker_row": case.get("tracker_row"),
        "ref_id": case.get("ref_id", ""),
        # Region + ops owner so outcome cards route back to the right person.
        "region": case.get("region", ""),
        "ops_dm_email": case.get("ops_dm_email", OPERATOR_EMAIL),
        "ops_dm_open_id": case.get("ops_dm_open_id", ""),
        "ops_dm_chat_id": case.get("ops_dm_chat_id", ""),
    }

    def btn(content, reply_type, btn_type):
        value = dict(base_value)
        value["reply_type"] = reply_type
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": content},
            "type": btn_type,
            "value": value,
        }

    header_title = f"📋 Spotify Reply Needed – {artist} – {upc}"

    detail_md = (
        f"**UPC:** {upc}\n"
        f"**ISRC:** `{isrc}`\n"
        f"**Title:** {title}\n"
        f"**Artist:** {artist}\n"
        f"**DSP:** {dsp}\n"
        f"**Claimant:** {claimant_name}\n"
        f"**Claimant Email:** {claimant_email}\n"
        f"**Date Detected:** {detected_at}\n"
        f"**Days Remaining:** {_countdown_label(days_remaining)}"
    )
    action_requested_md = f"**Action Requested:** {action_requested}"

    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": detail_md}},
            {"tag": "div", "text": {"tag": "lark_md", "content": action_requested_md}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "⚠️ **If no reply is sent before the deadline, the claim will be "
                "accepted as legitimate and content may be taken down.**"}},
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**CC / Extra recipient** *(optional)*"},
            },
            {
                "tag": "input",
                "name": "cc_recipient",
                "placeholder": {"tag": "plain_text", "content":
                    "email to CC on the reply — leave blank to skip"},
            },
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**Note to add** *(optional)*"},
            },
            {
                "tag": "input",
                "name": "note",
                "placeholder": {"tag": "plain_text", "content":
                    "extra text appended to the email body — leave blank to skip"},
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    btn("✅ Agree with claim", "agree", "primary"),
                    btn("🔍 Investigating – will follow up", "investigating", "default"),
                    btn("⚖️ Dispute claim", "dispute", "danger"),
                ],
            },
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content":
                    "Pick one action. All three options create a pre-made reply "
                    "draft back to the original claim email thread (preserving the "
                    "Spotify ref code for Salesforce threading). The draft stays in "
                    "the soundon-copyright mailbox for you to review and send."}
            ]},
        ],
    }
    return card


# ── Sender ───────────────────────────────────────────────────────────────────
def send_dm_action_card(case: dict, return_result: bool = False):
    """Send the DM action card to the case's Ops owner.

    Returns True/False by default for backward compatibility. When
    ``return_result=True`` it returns a dict with delivery details.

    The recipient is resolved from the case (``ops_dm_chat_id`` /
    ``ops_dm_open_id`` / ``ops_dm_email``) so each region routes to its own Ops
    person. When none are supplied it falls back to the default
    ``OPERATOR_EMAIL`` (BR/filipe.cairo).
    """
    from copyright_alert.bot_runtime import _post_api  # lazy import (avoids circular import)

    card = build_dm_action_card(case)
    content = json.dumps(card, ensure_ascii=False)

    recipient_email = case.get("ops_dm_email") or OPERATOR_EMAIL
    recipient_chat_id = case.get("ops_dm_chat_id") or ""
    recipient_open_id = case.get("ops_dm_open_id") or ""
    open_id = recipient_open_id or resolve_open_id(recipient_email)

    attempts = []
    if recipient_chat_id:
        attempts.append(("chat_id", recipient_chat_id))
    if open_id:
        attempts.append(("open_id", open_id))
    # Only fall back to email when it is a real, resolvable address.
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
                print(f"  ✓ DM action card sent via {id_type} ({rid}) → {mid} "
                      f"[UPC {case.get('upc')}]", flush=True)
                result = {
                    "ok": True,
                    "code": resp.get("code"),
                    "msg": resp.get("msg"),
                    "message_id": mid,
                    "receive_id_type": id_type,
                    "receive_id": rid,
                }
                return result if return_result else True
            print(f"  ✗ DM send via {id_type} returned code={resp.get('code')} msg={resp.get('msg')}", flush=True)
        except Exception as exc:
            print(f"  ✗ DM send via {id_type} failed: {exc!r}", flush=True)
    failure = {"ok": False, "code": None, "msg": "Failed to send DM action card", "message_id": ""}
    return failure if return_result else False


if __name__ == "__main__":
    # Manual test: python3 copyright_alert/dm_action_card.py '<case json>'
    if len(sys.argv) > 1:
        case = json.loads(sys.argv[1])
        print(send_dm_action_card(case))
    else:
        demo = {
            "upc": "047752352704", "isrc": "QT8BT2692502", "title": "Água Limpa",
            "artist": "Sena, DJ Marques, Kyan", "claimant_name": "Test Claimant",
            "claimant_email": "claimant@example.com", "source_email_message_id": "demo==",
            "lark_card_message_id": "om_demo", "detected_at": "2026-06-17", "tracker_row": 2,
        }
        print(json.dumps(build_dm_action_card(demo), ensure_ascii=False, indent=2))
