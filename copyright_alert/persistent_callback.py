#!/usr/bin/env python3
"""Persistent connection client for Lark card actions and slash-style chat commands."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lark_oapi as lark
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.api.im.v1.model.p2_im_chat_access_event_bot_p2p_chat_entered_v1 import P2ImChatAccessEventBotP2pChatEnteredV1
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
from lark_oapi.ws import Client as WsClient

from copyright_alert.bot_runtime import (
    BOT_SCRIPT,
    CHAT_TO_REGION,
    REGION_CONFIGS,
    attempt_self_heal,
    command_help_lines,
    ensure_daemon_alive,
    exception_lines,
    exclude_manager_lines,
    grouped_claim_lines,
    health_lines,
    include_manager_lines,
    notify_if_scan_running,
    parse_exclude_command,
    parse_include_command,
    parse_text_message,
    parse_upc_exclude_command,
    pending_lines,
    reply_post,
    reply_text,
    restart_daemon,
    start_scan_in_background,
    status_lines,
    unassigned_lines,
    upc_exclude_lines,
    upc_exclusion_lines,
    upc_unexclude_lines,
    write_pid_file,
)
from copyright_alert import run_alert as ra
from copyright_alert.dm_upc_lookup import is_upc, lookup_upc
from copyright_alert.dm_action_card import send_dm_action_card
from copyright_alert.handle_callback import (
    patch_message,
    read_sheet_values,
    update_card_state,
    update_sheet_status,
    update_sheet_email_status,
    reconstruct_card_from_tracker,
    _parse_lark_annotated_csv,
)
from copyright_alert import lark_auth, spotify_reply
from copyright_alert.run_alert import BOT_APP_ID, BOT_SECRET, load_posted_card


COMMAND_PREFIXES = ("/status", "/scan", "/pending", "/claims", "/restart", "/help", "/exclude", "/unexclude", "/exclusions", "/include", "/exceptions", "/unassigned", "/health", "/healthcheck", "/fix", "/refresh", "/card")
P2P_CHAT_CACHE_FILE = ROOT / "copyright_alert" / "bot_p2p_chats.json"
P2P_CHAT_CACHE_LOCK = threading.Lock()

# Bug 3: A DM action-card button click records the outcome in the tracker's
# Email Status column (T) but must ALSO reflect the operator's decision in the
# Status column (N). Map each reply action to the same Status vocabulary the
# group-card status buttons write (see run_alert.build_card status buttons), so
# both entry points keep column N consistent. "agree" means SoundOn accepts the
# claim, i.e. the content should be taken down.
STATUS_BY_REPLY_TYPE = {
    "agree": "🔴 Confirm Takedown",
    "investigating": "🔍 Investigating",
    "dispute": "⚖️ Disputing",
}

# ── Dispute button behavior (intentional) ────────────────────────────────────
# C2: The "⚖️ Dispute claim" button intentionally sends a *fixed* pre-made
# dispute template immediately (see handle_card_action → _process_spotify_reply
# with an empty custom_message, and spotify_reply.reply_dispute which ignores
# custom_message). There is deliberately NO conversational "type your custom
# dispute message" flow: input elements do not render reliably inside DM cards,
# so the previous PENDING_DISPUTES machinery could never be armed and was dead
# code. It has been removed to avoid confusion. If a custom-message flow is ever
# needed, re-introduce it explicitly and make reply_dispute honor custom_message.


def _obj_to_dict(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _obj_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_obj_to_dict(v) for v in obj]
    return {k: _obj_to_dict(v) for k, v in vars(obj).items() if not k.startswith("_")}


def _toast(text, typ="success"):
    return P2CardActionTriggerResponse({"toast": {"type": typ, "content": text}})


def _normalize_card_action_value(value):
    """Return the card action value as a dict.

    Lark normally delivers button values as objects, but some manually generated
    or older CardKit payloads can arrive as a JSON string. The callback handler
    reads value.get(...) before routing, so normalize defensively to avoid an
    AttributeError that causes Lark to show a generic backend-service error.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


def _refresh_callback_credentials(reason: str):
    """Refresh AIME-injected user credentials before callback subprocess/sheet work."""
    try:
        refreshed = lark_auth._refresh_aime_credentials()
        if refreshed:
            print(f"callback: refreshed {refreshed} AIME credential key(s) before {reason}", flush=True)
        return refreshed
    except Exception as exc:
        print(f"callback: credential refresh before {reason} failed: {exc!r}", flush=True)
        return 0


def _extract_user_open_id(user_id_obj):
    if user_id_obj is None:
        return ""
    if isinstance(user_id_obj, dict):
        return user_id_obj.get("open_id") or user_id_obj.get("user_id") or ""
    return getattr(user_id_obj, "open_id", None) or getattr(user_id_obj, "user_id", None) or ""


def _record_bot_p2p_chat(chat_id, operator_open_id, payload=None):
    if not chat_id:
        return
    record = {
        "chat_id": chat_id,
        "operator_open_id": operator_open_id or "",
        "last_seen_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with P2P_CHAT_CACHE_LOCK:
        try:
            current = json.loads(P2P_CHAT_CACHE_FILE.read_text(encoding="utf-8")) if P2P_CHAT_CACHE_FILE.exists() else {}
        except Exception:
            current = {}
        if not isinstance(current, dict):
            current = {}
        if operator_open_id:
            current.setdefault("by_open_id", {})[operator_open_id] = record
        current.setdefault("by_chat_id", {})[chat_id] = record
        P2P_CHAT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        from copyright_alert.run_alert import _atomic_write_json  # lazy: avoid import cycle
        _atomic_write_json(P2P_CHAT_CACHE_FILE, current, ensure_ascii=False, indent=2)
    print("bot_p2p_chat_entered recorded:", json.dumps(record, ensure_ascii=False), flush=True)


def handle_bot_p2p_chat_entered(data: P2ImChatAccessEventBotP2pChatEnteredV1):
    try:
        payload = _obj_to_dict(data)
        print("im.chat.access_event.bot_p2p_chat_entered_v1 payload:", json.dumps(payload, ensure_ascii=False), flush=True)
        event = getattr(data, "event", None)
        event_payload = (payload.get("event") or {}) if isinstance(payload, dict) else {}
        chat_id = (getattr(event, "chat_id", None) if event else None) or event_payload.get("chat_id")
        operator_id = (getattr(event, "operator_id", None) if event else None) or event_payload.get("operator_id")
        operator_open_id = _extract_user_open_id(operator_id)
        _record_bot_p2p_chat(chat_id, operator_open_id, payload)
    except Exception as exc:
        print("bot_p2p_chat_entered handler error:", repr(exc), flush=True)


def _trigger_event_driven_recovery(chat_id, reason):
    """Restart the callback daemon in response to an inline failure and notify
    only the chat where the failure was detected. Mirrors watchdog.py recovery
    but is triggered by event-driven failure signals (exceptions in the
    callback path) rather than a polling loop."""
    try:
        result = ensure_daemon_alive(
            notify_chat_id=chat_id,
            detected_via="button_click",
            send_notifications=True,
            force_restart=True,
            current_pid=os.getpid(),
        )
        print("event_driven_recovery:", json.dumps({"reason": str(reason), "result": result}, ensure_ascii=False, default=str), flush=True)
        return result
    except Exception as exc:
        print("event_driven_recovery error:", repr(exc), flush=True)
        return {"error": repr(exc)}


def _region_local_timestamp(region):
    """H3 (cosmetic): format an audit timestamp in the region's LOCAL time
    rather than the Shanghai-hosted runtime's naive local time.

    This is display-only. It does NOT touch deadline math (unified on BRT via
    C1) or any cron schedule — it only stops the status-audit line from showing
    Chinese time on a CST-hosted daemon. Falls back to a fixed BRT offset if the
    tz database is unavailable, and to naive local time as a last resort.
    """
    from datetime import timezone, timedelta
    try:
        cfg = (REGION_CONFIGS.get(str(region or "").strip().upper())
               or REGION_CONFIGS.get("BR") or {})
        tz_name = cfg.get("scan_local_tz") or "America/Sao_Paulo"
        try:
            from zoneinfo import ZoneInfo
            local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
            suffix = local.strftime("%Z") or ""
        except Exception:
            local = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3)))
            suffix = "BRT"
        return (local.strftime("%Y-%m-%d %H:%M ") + suffix).strip()
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _process_status_update(status, message_id, operator_name=None, operator_id=None, timestamp=None, upc=None, isrc=None, chat_id=None, region=None, tracker_row=None):
    try:
        _refresh_callback_credentials("callback tracker status write-back")
        card = load_posted_card(message_id)
        if not card:
            # No persisted copy of the clicked card. Rebuild it from the tracker
            # row rather than patching last_card.json, which is a *different*
            # claim's card and would silently overwrite this card with wrong
            # data (B8).
            card = reconstruct_card_from_tracker(
                message_id, region=region, upc=upc, isrc=isrc, tracker_row=tracker_row
            )
        if not card:
            msg = (
                "⚠️ Could not update this card: no saved copy exists and it could "
                "not be rebuilt from the tracker. Status was NOT changed — please "
                "retry, or update the tracker row manually."
            )
            print(f"_process_status_update: {msg} (message_id={message_id})", flush=True)
            if message_id:
                try:
                    reply_post(message_id, "Card update failed", [msg])
                except Exception as reply_exc:
                    print(f"_process_status_update: could not surface error: {reply_exc!r}", flush=True)
            return
        card = update_card_state(card, status, message_id, operator_name=operator_name, operator_id=operator_id, timestamp=timestamp)
        (ROOT / "copyright_alert/last_card_callback.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))
        patched = patch_message(message_id, card)
        sheet_ok = update_sheet_status(message_id, status, upc=upc, isrc=isrc, region=region, tracker_row=tracker_row)
        # NOTE: The button flow must ONLY write to column N (Status) via
        # update_sheet_status(). Column R ("Admin Action Taken") is manually
        # edited by ops and must NEVER be written by the bot, so we do not call
        # update_sheet_admin_action() here (F1).
        print(json.dumps({"patched": patched, "sheet_updated": sheet_ok, "status": status, "message_id": message_id, "operator": operator_name or operator_id, "timestamp": timestamp, "region": region, "tracker_row": tracker_row}, ensure_ascii=False), flush=True)
    except RuntimeError as exc:
        # G4: A RuntimeError here means a sheet-layout problem (e.g. a column
        # header was renamed, so update_sheet_status could not find the "Status"
        # column). This is NOT a daemon-health problem — restarting the daemon
        # would not fix it and would trigger an infinite restart/recovery loop.
        # Surface a clear message to the operator and do NOT restart the daemon.
        print("card.action.trigger sheet-layout error:", repr(exc), flush=True)
        if message_id:
            try:
                reply_post(
                    message_id,
                    "Tracker layout error",
                    ["⚠️ Tracker layout error — a column header may have been renamed. Please contact ops."],
                )
            except Exception as reply_exc:
                print(f"_process_status_update: could not surface layout error: {reply_exc!r}", flush=True)
    except Exception as exc:
        print("card.action.trigger background error:", repr(exc), flush=True)
        # Treat background processing failures as a signal that the daemon
        # may be unhealthy: trigger event-driven recovery and notify only the
        # chat where the button was clicked.
        if chat_id:
            _trigger_event_driven_recovery(chat_id, exc)


# ── Event dedup and outcome tracking ───────────────────────────────────────
# We track per-click event_ids for a short TTL so callback re-deliveries do not
# create duplicate reply drafts or duplicate error cards for the same click.
EVENT_LOCKS = {}
EVENT_LOCKS_LOCK = threading.Lock()
EVENT_DEDUP_TTL_SECONDS = 15 * 60


def _prune_event_locks(now=None):
    now = now or time.time()
    stale_ids = [
        event_id
        for event_id, touched_at in EVENT_LOCKS.items()
        if touched_at <= now - EVENT_DEDUP_TTL_SECONDS
    ]
    for event_id in stale_ids:
        EVENT_LOCKS.pop(event_id, None)


def _acquire_event_lock(event_id):
    if not event_id:
        return True
    now = time.time()
    with EVENT_LOCKS_LOCK:
        _prune_event_locks(now)
        if event_id in EVENT_LOCKS:
            return False
        EVENT_LOCKS[event_id] = now
        return True


def _release_event_lock(event_id):
    if not event_id:
        return
    with EVENT_LOCKS_LOCK:
        EVENT_LOCKS[event_id] = time.time()


def _process_spotify_reply(value, custom_message="", notify_chat_id=None, event_id=None, cc="", note=""):
    """Send the chosen Spotify reply to the original claim thread, then record the
    outcome in the tracker's 'Email Status' column. Runs in a background thread."""
    try:
        _refresh_callback_credentials("callback email-status write-back")
        reply_type = (value.get("reply_type") or "").strip().lower()
        tracker_row = value.get("tracker_row")
        # BRT (UTC-3) timestamp — the BR ops team works in São Paulo time, so the
        # tracker's Email Status column always shows BRT-local times.
        try:
            from datetime import timezone, timedelta
            brt_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3)))
            timestamp = brt_now.strftime("%Y-%m-%d %H:%M BRT")
        except Exception:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = spotify_reply.send_reply(
                reply_type=reply_type,
                source_email_message_id=(
                    value.get("source_email_message_id")
                    or value.get("source_message_id")
                    or ""
                ),
                claimant_email=value.get("claimant_email", ""),
                upc=value.get("upc", ""),
                title=value.get("title", ""),
                custom_message=custom_message,
                ref_id=value.get("ref_id", ""),
                cc=cc,
                note=note,
            )
            # Backwards-compat: spotify_reply.send_reply now returns a dict; tolerate
            # legacy bool return just in case.
            if isinstance(result, dict):
                ok = bool(result.get("ok"))
                mode = result.get("mode") or ("sent" if ok else "failed")
            else:
                ok = bool(result)
                mode = "sent" if ok else "failed"
                result = {"ok": ok, "mode": mode}
            if mode == "draft":
                # Success means we produced the operator-facing "Draft ready"
                # card that links to the shared mailbox inbox for manual review.
                mark = "Reply ready ✅"
            else:
                mark = "Failed ❌"
            status_value = f"{mark} – {reply_type} – {timestamp}"
            region = value.get("region")
            # H1.1: The draft has ALREADY been created by send_reply() above
            # (send_reply returns a result dict; it does not raise on the happy
            # path). A tracker write-back failure must NOT be reported as a
            # draft-creation failure — the old behavior surfaced the red "could
            # not create draft" card, which made operators retry and produced
            # duplicate/orphan drafts. Isolate the tracker write so any failure
            # here only downgrades the outcome to a green "Draft ready" card
            # carrying a warning that the status was not recorded.
            sheet_ok = False
            tracker_warning = ""
            try:
                sheet_ok = update_sheet_email_status(tracker_row, status_value, region=region)
                if not sheet_ok:
                    tracker_warning = (
                        "⚠️ Could not record this in the tracker — status not updated."
                    )
            except Exception as sheet_exc:
                print("spotify_reply tracker write-back error (draft still created):",
                      repr(sheet_exc), flush=True)
                print(traceback.format_exc(), flush=True)
                tracker_warning = (
                    "⚠️ Could not record this in the tracker — status not updated."
                )
            # Bug 3: also reflect the operator's decision in column N ("Status").
            # Previously only Email Status (column T) was written, so a case that
            # was clearly acted on from the DM action card still showed a blank
            # Status. Only write on the happy path (a draft was created) and keep
            # this isolated so a Status write failure never turns a successful
            # draft into a red failure card.
            status_written = None
            if mode == "draft":
                mapped_status = STATUS_BY_REPLY_TYPE.get(reply_type)
                if mapped_status:
                    try:
                        status_written = update_sheet_status(
                            value.get("lark_card_message_id", ""),
                            mapped_status,
                            upc=value.get("upc"),
                            isrc=value.get("isrc"),
                            region=region,
                            tracker_row=tracker_row,
                        )
                        print(
                            f"spotify_reply Status (col N) write-back "
                            f"({reply_type} -> {mapped_status}): {status_written}",
                            flush=True,
                        )
                        if not status_written and not tracker_warning:
                            tracker_warning = (
                                "⚠️ Could not record this in the tracker — status not updated."
                            )
                    except Exception as status_exc:
                        print("spotify_reply Status (col N) write-back error "
                              "(draft still created):", repr(status_exc), flush=True)
                        print(traceback.format_exc(), flush=True)
                        if not tracker_warning:
                            tracker_warning = (
                                "⚠️ Could not record this in the tracker — status not updated."
                            )
            send_preview_url = result.get("send_preview_url", "") if isinstance(result, dict) else ""
            print(json.dumps({
                "spotify_reply": reply_type,
                "reply_ok": ok,
                "reply_mode": mode,
                "email_status_written": sheet_ok,
                "status_col_written": status_written,
                "tracker_row": tracker_row,
                "upc": value.get("upc"),
                "message_id": result.get("message_id") if isinstance(result, dict) else "",
                "draft_id": result.get("draft_id") if isinstance(result, dict) else "",
                "send_preview_url": send_preview_url,
                "error": result.get("error") if isinstance(result, dict) else None,
            }, ensure_ascii=False), flush=True)
            # Always notify the operator with a CARD (never plain text).
            # On success this is the product-approved "Draft ready" card with a
            # single shared-mailbox review button.
            try:
                _send_outcome_card(
                    chat_id=notify_chat_id,
                    reply_type=reply_type,
                    value=value,
                    mode=mode,
                    draft_url=send_preview_url,
                    error_detail=(result.get("error") if isinstance(result, dict) else None),
                    tracker_warning=tracker_warning,
                )
            except Exception as exc:
                print("spotify_reply outcome-card send error:", repr(exc), flush=True)
        except Exception as exc:
            print("spotify_reply background error:", repr(exc), flush=True)
            print(traceback.format_exc(), flush=True)
            try:
                update_sheet_email_status(
                    tracker_row, f"Failed ❌ – {reply_type} – {timestamp}", region=value.get("region"))
            except Exception as exc2:
                print("spotify_reply failure-status write error:", repr(exc2), flush=True)
            # Even when the worker itself crashes, surface a card to the operator.
            try:
                _send_outcome_card(
                    chat_id=notify_chat_id,
                    reply_type=reply_type,
                    value=value,
                    mode="failed",
                    draft_url="",
                    error_detail=f"Background worker crashed: {exc!r}",
                )
            except Exception as exc3:
                print("spotify_reply crash-card error:", repr(exc3), flush=True)
    finally:
        if event_id:
            _release_event_lock(event_id)


def _send_chat_text(chat_id, text):
    """Send a plain-text DM/message to a chat using the bot identity."""
    from copyright_alert.bot_runtime import _post_api  # lazy import (avoid cycle)
    return _post_api(
        "/im/v1/messages?receive_id_type=chat_id",
        {"receive_id": chat_id, "msg_type": "text",
         "content": json.dumps({"text": text}, ensure_ascii=False)},
    )


def _send_outcome_card(chat_id, reply_type, value, mode, draft_url="",
                       error_detail=None, tracker_warning=""):
    """Post an outcome card after a Spotify reply attempt.

    Two visual variants:

      • mode="draft"
          Green header "✉️ Draft ready – <action>" with a primary
          "🔗 Review & Send Draft" button linking to draft_url. The operator
          opens the draft in Lark Mail and clicks Send manually.

          When `tracker_warning` is supplied (H1.1), the draft still succeeded
          but the tracker Email Status write-back failed — the warning is
          appended to the green card body so the operator knows the draft is
          real (do NOT retry) but the tracker row was not updated.

      • anything else (treated as failure)
          Red header "❌ Draft failed – <specific cause>" with the underlying
          error text in the body, plus a "🔄 Click here to retry" button that
          points back to the original DM action card (best-effort).

    The card is delivered to `chat_id` when provided; otherwise it falls back
    to a DM to OPERATOR_EMAIL. There is no plain-text fallback — the response
    is always a card per product spec.
    """
    from copyright_alert.bot_runtime import _post_api  # lazy import (avoid cycle)
    from copyright_alert.dm_action_card import resolve_open_id, OPERATOR_EMAIL

    upc = value.get("upc") or "N/A"
    title = value.get("title") or "N/A"
    artist = value.get("artist") or "N/A"

    # ── Pick the header + body framing based on outcome ──────────────────────
    if mode == "draft":
        template = "green"
        header_title = f"✉️ Draft ready – {reply_type}"
        intro = (
            f"A '{reply_type}' draft was created in the `soundon-copyright` "
            "mailbox. Click the button below to review the draft and send it "
            "from your Lark mailbox."
        )
        if draft_url:
            cta = {
                "tag": "button",
                "text": {"tag": "plain_text",
                         "content": "🔗 Review & Send Draft"},
                "type": "default",
                "url": draft_url,
            }
        else:
            # Draft was created but we didn't get a preview URL back. Surface
            # the draft_id-less state honestly rather than offering a retry.
            cta = None
            intro += (
                "\n\n⚠️ The draft URL was not returned by Lark Mail — please "
                "open the `soundon-copyright` mailbox drafts folder manually."
            )
        # H1.1: the draft is real even though the tracker row could not be
        # updated. Tell the operator NOT to retry (retrying makes orphan drafts).
        if tracker_warning:
            intro += (
                f"\n\n{tracker_warning} The draft above is valid — please do "
                "**not** click the button again; update the tracker row "
                "manually if needed."
            )
    else:
        template = "red"
        # Build a short, specific failure tag for the header from error_detail.
        cause = _summarize_cause(error_detail) or "unknown error"
        header_title = f"⚠️ Draft could not be created – {cause}"

        # Clean up error_detail: remove raw stdout/stderr dumps and noisy JSON blocks
        friendly_error = str(error_detail or 'No additional details available.')
        if "STDOUT:" in friendly_error:
            friendly_error = friendly_error.split("STDOUT:")[0].strip()
        if "lark-cli" in friendly_error.lower() and "rc=" in friendly_error.lower():
             # Extract just the rc message if it exists
             rc_match = re.search(r"failed \(rc=\d+\)", friendly_error)
             if rc_match:
                 friendly_error = f"The underlying system (lark-cli) reported a failure: {rc_match.group(0)}"

        intro = (
            f"The bot could not create the **'{reply_type}'** reply draft.\n\n"
            f"**Reason:** {friendly_error}\n\n"
            "💡 **If this keeps failing, type `/refresh` in your chat with the bot to restore the token.**"
        )
        cta = {
            "tag": "button",
            "text": {"tag": "plain_text",
                     "content": "🔄 Click here to retry"},
            "type": "primary",
            "value": {**value, "action": "spotify_reply"},
        }

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content":
            f"{intro}\n\n"
            f"**Release:** {title}\n"
            f"**Artist:** {artist}\n"
            f"**UPC:** {upc}"}},
    ]
    if cta is not None:
        elements.append({"tag": "action", "actions": [cta]})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }
    content = json.dumps(card, ensure_ascii=False)

    # Delivery: prefer the chat where the click happened.
    if chat_id:
        try:
            resp = _post_api(
                "/im/v1/messages?receive_id_type=chat_id",
                {"receive_id": chat_id, "msg_type": "interactive",
                 "content": content},
            )
            if resp.get("code") == 0:
                mid = ((resp.get("data") or {}).get("message_id")) or ""
                print(f"  ✓ outcome card sent to chat {chat_id} → {mid} "
                      f"(mode={mode})", flush=True)
                return True
            print(f"  ✗ outcome card via chat_id code={resp.get('code')} "
                  f"msg={resp.get('msg')}", flush=True)
        except Exception as exc:
            print(f"  ✗ outcome card via chat_id failed: {exc!r}", flush=True)

    # Fallback: DM the region's Ops owner directly. The action value carries
    # the ops recipient so SPLA outcome cards never fall back to filipe.cairo.
    recipient_email = value.get("ops_dm_email") or OPERATOR_EMAIL
    recipient_chat_id = value.get("ops_dm_chat_id") or ""
    recipient_open_id = value.get("ops_dm_open_id") or ""
    open_id = recipient_open_id or resolve_open_id(recipient_email)
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
                print(f"  ✓ outcome card DM sent via {id_type} ({rid}) → {mid} "
                      f"(mode={mode})", flush=True)
                return True
            print(f"  ✗ outcome card DM via {id_type} code={resp.get('code')} "
                  f"msg={resp.get('msg')}", flush=True)
        except Exception as exc:
            print(f"  ✗ outcome card DM via {id_type} failed: {exc!r}",
                  flush=True)
    return False


def _summarize_cause(error_detail) -> str:
    """Map a verbose error_detail to a short header-friendly cause label."""
    if not error_detail:
        return ""
    txt = str(error_detail).lower()
    if "missing source email message id" in txt or "missing ref" in txt:
        return "missing source message ID"
    if "missing claimant email" in txt:
        return "missing claimant email"
    if "token expired" in txt or "jwt token expired" in txt or "expired" in txt and "jwt" in txt:
        return "token expired"
    if "rate limit" in txt or "429" in txt:
        return "rate limited"
    if "timeout" in txt or "timed out" in txt:
        return "network timeout"
    if "invalid recipient" in txt or "invalid email" in txt:
        return "invalid recipient"
    if "permission denied" in txt or "forbidden" in txt or "403" in txt:
        return "permission denied"
    if "background worker crashed" in txt:
        return "internal worker crash"
    if "unknown reply type" in txt:
        return "unknown reply type"
    return "lark-cli error"


def handle_card_action(data):
    chat_id = None
    try:
        payload = _obj_to_dict(data)
        print("card.action.trigger payload:", json.dumps(payload, ensure_ascii=False), flush=True)
        event = getattr(data, "event", None)
        action = getattr(event, "action", None) if event else None
        context = getattr(event, "context", None) if event else None
        value = getattr(action, "value", None) if action else None
        value = _normalize_card_action_value(
            value if value not in (None, "") else (((payload.get("event") or {}).get("action") or {}).get("value") or {})
        )
        # Optional form inputs (CC / note) submitted alongside a card button.
        form_value = getattr(action, "form_value", None) if action else None
        form_value = form_value or (((payload.get("event") or {}).get("action") or {}).get("form_value") or {})
        if not isinstance(form_value, dict):
            form_value = {}
        ctx_payload = ((payload.get("event") or {}).get("context") or {})
        header = getattr(data, "header", None)
        header_payload = payload.get("header") or {}
        event_id = (
            (getattr(header, "event_id", None) if header else None)
            or header_payload.get("event_id")
            or (getattr(event, "event_id", None) if event else None)
        )
        chat_id = (
            (getattr(context, "open_chat_id", None) if context else None)
            or ctx_payload.get("open_chat_id")
            or value.get("chat_id")
        )

        # ── Send Ops DM from group card ───────────────────────────────────────
        if isinstance(value, dict) and value.get("action") == "send_ops_dm":
            if event_id and not _acquire_event_lock(event_id):
                print(f"Skipping duplicate send_ops_dm action for event_id: {event_id}", flush=True)
                return _toast("This button click was already processed.", "warn")

            try:
                from copyright_alert.dm_action_card import send_dm_action_card

                case = {
                    "upc": value.get("upc", "N/A"),
                    "isrc": value.get("isrc", "N/A"),
                    "title": value.get("title", "N/A"),
                    "artist": value.get("artist", "N/A"),
                    "claimant_name": value.get("claimant_name", "N/A"),
                    "claimant_email": value.get("claimant_email", "N/A"),
                    "source_email_message_id": (
                        value.get("source_email_message_id")
                        or value.get("source_message_id")
                        or ""
                    ),
                    "lark_card_message_id": value.get("lark_card_message_id", ""),
                    "tracker_row": value.get("tracker_row", ""),
                    "ref_id": value.get("ref_id", ""),
                    "region": value.get("region", ""),
                    "ops_dm_email": value.get("ops_dm_email", ""),
                    "ops_dm_open_id": value.get("ops_dm_open_id", ""),
                    "ops_dm_chat_id": value.get("ops_dm_chat_id", ""),
                }
                result = send_dm_action_card(case, return_result=True)
                if result.get("ok"):
                    return _toast("Private Ops DM card sent.")
                return _toast(f"Failed to send Ops DM card: {result.get('msg') or 'unknown error'}", "error")
            except Exception as exc:
                print(f"send_ops_dm callback error: {exc!r}", flush=True)
                return _toast(f"Failed to send Ops DM card: {exc}", "error")

        # ── Spotify reply action (DM action card) ────────────────────────────
        if isinstance(value, dict) and value.get("action") == "spotify_reply":
            reply_type = (value.get("reply_type") or "").strip().lower()

            # ── Dedup button clicks ──
            if event_id and not _acquire_event_lock(event_id):
                print(f"Skipping duplicate card action for event_id: {event_id}", flush=True)
                return _toast("This button click was already processed.", "warn")

            # Optional operator-supplied fields from the card form. Both are
            # ignored when blank, so the normal flow is unchanged.
            cc_recipient = str(form_value.get("cc_recipient") or value.get("cc_recipient") or "").strip()
            note_text = str(form_value.get("note") or value.get("note") or "").strip()

            # Dispute now uses a fixed pre-made template (no custom message),
            # so it sends immediately just like agree/investigating.
            # Agree / Investigating / Dispute all send a pre-made reply immediately.
            worker = threading.Thread(
                target=_process_spotify_reply,
                args=(value, "", chat_id, event_id),
                kwargs={"cc": cc_recipient, "note": note_text},
                daemon=True,
            )
            worker.start()
            return _toast(f"Creating '{reply_type}' draft... check your DMs.", "success")

        clicked_status = value.get("status")
        current_status = (value.get("current_status") or "").strip()
        status = "" if current_status and current_status == clicked_status else clicked_status
        message_id = value.get("message_id") or (getattr(context, "open_message_id", None) if context else None) or ctx_payload.get("open_message_id")
        if clicked_status is None or not message_id:
            print("missing status/message_id", json.dumps({"value": value, "message_id": message_id}, ensure_ascii=False), flush=True)
            return _toast("Missing status or message_id in callback", "error")

        operator = getattr(event, "operator", None) if event else None
        operator_payload = ((payload.get("event") or {}).get("operator") or {})
        operator_id = (getattr(operator, "open_id", None) if operator else None) or operator_payload.get("open_id") or operator_payload.get("user_id")
        operator_name = operator_payload.get("name") or operator_payload.get("open_id") or operator_payload.get("user_id")
        upc = value.get("upc")
        isrc = value.get("isrc")
        region = value.get("region") or CHAT_TO_REGION.get(chat_id or "")
        # H3 (cosmetic): stamp the status-audit line in the region's local time
        # instead of the daemon host's naive (Shanghai) time.
        timestamp = _region_local_timestamp(region)
        tracker_row = value.get("tracker_row")

        worker = threading.Thread(
            target=_process_status_update,
            args=(status, message_id, operator_name, operator_id, timestamp, upc, isrc, chat_id, region, tracker_row),
            daemon=True,
        )
        worker.start()
        return _toast("Status reset to No action yet" if not status else f"Status update received: {status}")
    except Exception as exc:
        print("card.action.trigger error:", repr(exc), flush=True)
        print(traceback.format_exc(), flush=True)
        # Inline event-driven recovery: a failure here is treated as the
        # daemon being unresponsive. Restart and post a recovery notice only
        # to the originating chat, then acknowledge the click so the user
        # knows to retry.
        if chat_id:
            _trigger_event_driven_recovery(chat_id, exc)
        return _toast(f"Callback failed ({exc}); daemon recovery triggered, please retry.", "error")


def _posted_claim_record_for_upc(upc: str):
    upc = str(upc or "").strip()
    if not upc:
        return {}
    try:
        from copyright_alert import run_alert as ra
        posted = ra._load_posted_claims()
    except Exception as exc:
        print(f"/card: load posted_claims failed for {upc}: {exc!r}", flush=True)
        return {}
    if not isinstance(posted, dict):
        return {}
    for record in posted.values():
        if isinstance(record, dict) and str(record.get("upc") or "").strip() == upc:
            return record
    return {}


def _is_inbound_claim_subject(subject: str) -> bool:
    subject = str(subject or "").strip().lower()
    if not subject:
        return False
    return not (subject.startswith("re:") or subject.startswith("fw:") or subject.startswith("fwd:"))


def _enrich_case_for_reply(case: dict) -> dict:
    """Populate claimant/source-email fields for DM action cards.

    Priority order:
      1. Canonical posted_claims record for the UPC (daily scan source of truth)
      2. Inbox triage fallback, but only on inbound/non-reply subjects
    """
    case = dict(case or {})
    upc = str(case.get("upc") or "").strip()
    if not upc:
        return case

    posted = _posted_claim_record_for_upc(upc)
    if posted:
        for key in ("source_email_message_id", "claimant_email", "ref_id", "claimant_name", "title", "artist"):
            value = posted.get(key)
            if value not in (None, "", "N/A"):
                case[key] = value
        print(
            f"/card enrichment: using posted_claims record for {upc} "
            f"(source_email_message_id={case.get('source_email_message_id')!r}, claimant_email={case.get('claimant_email')!r})",
            flush=True,
        )
        return case

    try:
        from copyright_alert import dm_upc_lookup as dul
        from copyright_alert import run_alert as ra
        msgs = [m for m in dul._triage_search(upc) if _is_inbound_claim_subject((m or {}).get("subject", ""))]
        for msg in msgs:
            mid = (msg or {}).get("message_id", "")
            if not mid:
                continue
            body, meta = ra.fetch_email(mid)
            ef = ra.extract_fields(body, meta.get("subject", ""), meta)
            claimant_email = ef.get("claimant_email")
            if claimant_email in (None, "", "N/A"):
                continue
            case["source_email_message_id"] = mid
            case["claimant_email"] = claimant_email
            if ef.get("ref_id") not in (None, "", "N/A"):
                case["ref_id"] = ef["ref_id"]
            if ef.get("claimant_name") not in (None, "", "N/A"):
                case["claimant_name"] = ef["claimant_name"]
            if case.get("title") in (None, "", "N/A") and ef.get("title") not in (None, "", "N/A"):
                case["title"] = ef["title"]
            print(
                f"/card enrichment: using inbound triage hit for {upc} "
                f"(source_email_message_id={mid!r}, claimant_email={claimant_email!r})",
                flush=True,
            )
            break
    except Exception as exc:
        print(f"/card enrichment failed for {upc}: {exc!r}", flush=True)
    return case


def _read_tracker_fresh(region: str):
    """Read tracker sheet through a short-lived lark-cli process.

    The persistent callback daemon can keep an expired AIME JWT in its long-lived
    environment. Keeping the tracker read isolated here avoids the older bot-token
    API path and matches the daily scan's lark-cli CSV-read format.
    """
    cfg = REGION_CONFIGS.get(str(region or "").upper(), {})
    tracker_url = cfg.get("tracker_url", "")
    sheet_id = cfg.get("sheet_id", "")
    if not tracker_url or not sheet_id:
        raise ValueError(f"No tracker config for region {region!r}")
    cmd = [
        "lark-cli", "sheets", "+csv-get",
        "--url", tracker_url,
        "--sheet-id", sheet_id,
        "--range", "A1:T2000",
        "--max-chars", "200000",
    ]
    # Refresh AIME-injected credentials in-place before spawning lark-cli. The
    # daemon is long-lived, so its os.environ can otherwise keep stale JWT values
    # that are inherited by subprocesses and make tracker reads fail even after a
    # daemon restart.
    _refresh_callback_credentials("lark-cli sheet read")
    env = os.environ.copy()
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)
    combined = (res.stdout or "") + (res.stderr or "")
    # B7: keep row_numbers so callers can map a list index back to the real
    # sheet row number (blank/skipped rows make `idx + 1` wrong).
    parsed, rows, row_numbers = ra.parse_lark_annotated_csv(res.stdout)
    if res.returncode != 0 or not parsed:
        raise RuntimeError(f"lark-cli sheet read failed: {combined[:500]}")
    return rows, row_numbers


def _handle_card_command(command_text, message_id, target_chat_id="", target_open_id="", region_hint=None):
    """Handle `/card <UPC> [region]`: look up the UPC in the trackers and DM the
    invoking operator a private Spotify action card for that case.

    Works in a DM (card goes to the current p2p chat) and in a group chat (card
    is DM'd to the operator via their open_id). Runs in a background thread.
    """
    try:
        _, arg = _command_args(command_text)
        upc = ""
        region_flag = ""
        for tok in (arg or "").split():
            t = tok.strip()
            if not t:
                continue
            if re.fullmatch(r"\d{10,13}", t):
                upc = t
            elif t.upper() in REGION_CONFIGS:
                region_flag = t.upper()
        if not upc:
            reply_post(message_id, "/card", [
                "Usage: `/card <UPC> [region]`",
                "Example: `/card 0850053152337` or `/card 0850053152337 US`",
                "Regions: BR, SPLA, US (auto-detected across all trackers if omitted).",
            ])
            return

        if region_flag:
            regions = [region_flag]
        elif region_hint and str(region_hint).upper() in REGION_CONFIGS:
            primary = str(region_hint).upper()
            regions = [primary] + [r for r in REGION_CONFIGS if r != primary]
        else:
            regions = list(REGION_CONFIGS.keys())

        found = None
        for region in regions:
            try:
                rows, row_numbers = _read_tracker_fresh(region)
            except Exception as exc:
                print(f"/card: read tracker {region} via fresh subprocess failed: {exc!r}", flush=True)
                continue
            for idx, row in enumerate(rows):
                if row and str(row[0]).strip() == upc:
                    # B7: map the list index back to the true sheet row number
                    # via row_numbers. `idx + 1` is wrong whenever the CSV
                    # reader skipped blank rows; row_numbers already accounts
                    # for the header row and any gaps.
                    sheet_row = row_numbers[idx] if idx < len(row_numbers) else idx + 1
                    found = (region, sheet_row, row)
                    break
            if found:
                break

        if not found:
            reply_post(message_id, "/card", [
                f"❌ UPC `{upc}` was not found in any tracker (BR / SPLA / US).",
                "Double-check the UPC, or run `/scan` if this is a brand-new claim.",
            ])
            return

        region, row_num, row = found

        def cell(i):
            try:
                return str(row[i]).strip()
            except Exception:
                return ""

        cfg = REGION_CONFIGS.get(region, {})
        case = {
            "upc": cell(0) or upc,
            "isrc": cell(1) or "N/A",
            "title": cell(2) or "N/A",
            "artist": cell(7) or "N/A",
            "claimant_name": cell(9) or "N/A",
            "claimant_email": "N/A",
            "detected_at": cell(14) or "",
            "source_email_message_id": "",
            "ref_id": "",
            "region": region,
            "tracker_row": row_num,
            "ops_dm_email": cfg.get("ops_dm_email", ""),
            "ops_dm_open_id": target_open_id or "",
            "ops_dm_chat_id": target_chat_id or "",
        }

        case = _enrich_case_for_reply(case)

        result = send_dm_action_card(case, return_result=True)
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        if not ok:
            err = result.get("error") if isinstance(result, dict) else "unknown error"
            reply_post(message_id, "/card", [
                f"⚠️ Found UPC `{upc}` ({region}) but could not deliver the DM card: {err}",
            ])
    except Exception as exc:
        print(f"/card handler error: {exc!r}", flush=True)
        try:
            reply_post(message_id, "/card", [f"❌ /card failed: {exc}"])
        except Exception:
            pass


def _normalize_command(text: str) -> str:
    return (text or "").strip()


def _command_args(command_text: str):
    parts = _normalize_command(command_text).split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def _handle_command(command_text: str, message_id: str, region: str) -> None:
    cmd, arg = _command_args(command_text)
    try:
        if cmd == "/help":
            reply_post(message_id, "/help", command_help_lines())
        elif cmd == "/status":
            notify_if_scan_running(message_id)
            reply_post(message_id, "/status", status_lines(region))
        elif cmd == "/pending":
            # Optional argument: account manager name to filter by.
            notify_if_scan_running(message_id)
            reply_post(message_id, "/pending", pending_lines(region, arg or None))
        elif cmd == "/claims":
            notify_if_scan_running(message_id)
            title, lines = grouped_claim_lines(region, arg or None)
            reply_post(message_id, title, lines)
        elif cmd == "/scan":
            reply_post(message_id, f"/scan — {region}", [f"Starting an immediate {region} scan now…"])
            start_scan_in_background(region, message_id)
        elif cmd == "/restart":
            new_pid = restart_daemon(current_pid=os.getpid())
            reply_post(message_id, "/restart", [f"🔄 Restarting callback daemon... ✅ Done! PID: {new_pid}"])
        elif cmd == "/exclude":
            arg_str = (arg or "").strip()
            first_token = arg_str.split()[0] if arg_str else ""
            # F2: Route by the FIRST token. A UPC exclusion is only valid when
            # the first token is a 12–13 digit UPC. Anything else (contains
            # "from", starts with "@", or a plain name) is a manager exclusion
            # attempt, and on parse failure we show the MANAGER usage message
            # (not the confusing UPC one).
            looks_like_upc = bool(re.fullmatch(r"\d{12,13}", first_token))
            if looks_like_upc:
                upc, reason = parse_upc_exclude_command(arg)
                title, lines = upc_exclude_lines(upc, reason, added_by="chat_command")
            else:
                manager, label_uid = parse_exclude_command(arg)
                if manager and label_uid:
                    title, lines = exclude_manager_lines(manager, label_uid)
                else:
                    # Manager-style intent that failed to parse → show the
                    # MANAGER usage message (not the UPC one).
                    title = "/exclude — manager exclusion"
                    lines = [
                        "⚠️ Could not parse that exclusion.",
                        "",
                        "**Exclude a manager from a label's alerts:**",
                        "`/exclude @Manager Name from <UID>`",
                        "",
                        "**Exclude a release by UPC:**",
                        "`/exclude <12–13 digit UPC> [reason]`",
                    ]
            reply_post(message_id, title, lines)
        elif cmd == "/unexclude":
            title, lines = upc_unexclude_lines(arg)
            reply_post(message_id, title, lines)
        elif cmd == "/exclusions":
            title, lines = upc_exclusion_lines()
            reply_post(message_id, title, lines)
        elif cmd == "/include":
            manager, label_uid = parse_include_command(arg)
            title, lines = include_manager_lines(manager, label_uid)
            reply_post(message_id, title, lines)
        elif cmd == "/exceptions":
            title, lines = exception_lines(arg or "")
            reply_post(message_id, title, lines)
        elif cmd == "/unassigned":
            notify_if_scan_running(message_id)
            target_region = (arg or region or "BR").upper()
            title, lines = unassigned_lines(target_region)
            reply_post(message_id, title, lines)
        elif cmd == "/health":
            title, lines = health_lines()
            reply_post(message_id, title, lines)
        elif cmd == "/fix":
            title, lines = attempt_self_heal(current_pid=os.getpid())
            reply_post(message_id, title, lines)
        elif cmd == "/refresh":
            # 1. Force reload tokens from the AIME env refresh file. Use the
            #    real refresh path in lark_auth (spotify_reply._refresh_aime_credentials
            #    is a documented legacy no-op that always returns 0).
            try:
                refreshed_keys = lark_auth._refresh_aime_credentials()
            except Exception as refresh_exc:
                refreshed_keys = None
                print(f"/refresh credential refresh failed: {refresh_exc!r}", flush=True)
            if isinstance(refreshed_keys, int):
                refresh_summary = f"🔄 Refreshed {refreshed_keys} credential key(s)."
            else:
                refresh_summary = "🔄 Tokens refreshed."
            # 2. Re-read health check to see if JWT is actually OK now
            from copyright_alert.bot_runtime import run_health_check
            report = run_health_check()
            jwt_status = (report.get("jwt") or {}).get("status", "err")
            if jwt_status == "ok":
                msg = f"{refresh_summary} JWT OK and daemon restarted! ✅"
            else:
                detail = (report.get("jwt") or {}).get("detail", "unknown error")
                msg = f"{refresh_summary}\n⚠️ Refresh attempted but JWT is still invalid: {detail}\n\nManual AIME environment refresh by Filipe may be required."

            # 3. Restart daemon
            new_pid = restart_daemon(current_pid=os.getpid())
            reply_post(message_id, "/refresh", [f"{msg}\n✅ New PID: {new_pid}"])
        elif cmd == "/healthcheck" or cmd == "/health":
            from copyright_alert.bot_runtime import health_lines
            title, lines = health_lines()
            reply_post(message_id, title, lines)
    except Exception as exc:
        reply_post(message_id, f"{cmd or '/command'} failed", [repr(exc)])


def _handle_dm_upc(upc: str, message_id: str) -> None:
    try:
        reply = lookup_upc(upc)
    except Exception as exc:
        reply = f"UPC lookup failed: {exc!r}"
    try:
        reply_text(message_id, reply)
    except Exception as exc:
        print("dm upc reply error:", repr(exc), flush=True)


# Find UPC-like sequences (12 or 13 consecutive digits) anywhere in a message.
# Use word-boundary-ish guards so we don't match inside longer numbers.
UPC_SCAN = re.compile(r"(?<!\d)(\d{12,13})(?!\d)")


def _extract_upcs(text: str):
    """Return ordered, de-duplicated list of UPC candidates found in text."""
    if not text:
        return []
    seen = set()
    out = []
    for m in UPC_SCAN.finditer(text):
        upc = m.group(1)
        if upc in seen:
            continue
        seen.add(upc)
        out.append(upc)
    return out


def _handle_dm_upcs(upcs, message_id: str) -> None:
    """Look up one or more UPCs and reply with combined results."""
    sections = []
    for upc in upcs:
        try:
            res = lookup_upc(upc)
        except Exception as exc:
            res = f"UPC lookup failed: {exc!r}"
        if len(upcs) == 1:
            sections.append(res)
        else:
            sections.append(f"━━━ UPC {upc} ━━━\n{res}")
    body = "\n\n".join(sections) if sections else "No results."
    try:
        reply_text(message_id, body)
    except Exception as exc:
        print("dm upc reply error:", repr(exc), flush=True)


def handle_message_receive(data: P2ImMessageReceiveV1):
    try:
        payload = _obj_to_dict(data)
        print("im.message.receive payload:", json.dumps(payload, ensure_ascii=False), flush=True)
        event = getattr(data, "event", None)
        if not event:
            return
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if not message or getattr(message, "message_type", None) != "text":
            return
        if getattr(sender, "sender_type", None) == "bot":
            return
        chat_id = getattr(message, "chat_id", None)
        chat_type = getattr(message, "chat_type", None)
        message_id = getattr(message, "message_id", "")
        text = parse_text_message(getattr(message, "content", ""))

        # Resolve the sender's open_id (used to key DM command handling).
        sender_open_id = None
        sender_id = getattr(sender, "sender_id", None) if sender else None
        if sender_id is not None:
            sender_open_id = getattr(sender_id, "open_id", None) or getattr(sender_id, "user_id", None)

        # P2P (private DM): natural-language UPC extraction (supports multiple UPCs).
        if chat_type == "p2p":
            stripped = (text or "").strip()

            # C2: There is intentionally no conversational dispute-capture flow
            # here. The Dispute button sends a fixed template immediately, so a
            # plain DM is never interpreted as a "custom dispute message".
            upcs = _extract_upcs(stripped)
            stripped_lower = stripped.lower()
            if stripped_lower.startswith("/card"):
                # `/card <UPC>` in a DM → send the action card to this DM chat.
                worker = threading.Thread(
                    target=_handle_card_command,
                    args=(stripped, message_id, chat_id, sender_open_id, "BR"),
                    daemon=True,
                )
                worker.start()
                return
            if (stripped_lower.startswith("/unassigned") or stripped_lower.startswith("/pending")
                    or stripped_lower.startswith("/help") or stripped_lower.startswith("/health")
                    or stripped_lower.startswith("/exclude") or stripped_lower.startswith("/unexclude")
                    or stripped_lower.startswith("/exclusions")
                    or stripped_lower.startswith("/fix") or stripped_lower.startswith("/refresh")):
                # Slash command in DM — default to BR region; allow override
                # like `/unassigned US` or `/pending @ManagerName`.
                worker = threading.Thread(
                    target=_handle_command,
                    args=(stripped, message_id, "BR"),
                    daemon=True,
                )
                worker.start()
                return
            if upcs:
                # Immediate ack so the user knows the bot received the message
                # before the (potentially slow) inbox lookup runs.
                if len(upcs) == 1:
                    ack = f"🔍 Searching for UPC {upcs[0]}..."
                else:
                    ack = "🔍 Searching for UPCs: " + ", ".join(upcs) + "..."
                try:
                    reply_text(message_id, ack)
                except Exception as exc:
                    print("dm ack reply error:", repr(exc), flush=True)
                worker = threading.Thread(target=_handle_dm_upcs, args=(upcs, message_id), daemon=True)
                worker.start()
            else:
                # Friendly hint for non-UPC DMs so users know what to send.
                # Always reply (even for slash-prefixed text in DMs) so the
                # user gets immediate feedback.
                if stripped:
                    try:
                        reply_text(
                            message_id,
                            "Hi! Send me a UPC (12-13 digit number) — you can paste it on its own or include it in a sentence (e.g. \"is 5063964388275 offline?\"). I'll look up matching infringement claim emails. You can also send multiple UPCs in one message.",
                        )
                    except Exception as exc:
                        print("dm hint reply error:", repr(exc), flush=True)
            return

        # Group chats: existing slash-command behavior.
        if chat_id not in CHAT_TO_REGION:
            return
        if not text.startswith("/"):
            return
        normalized = _normalize_command(text)
        if not any(normalized.lower().startswith(prefix) for prefix in COMMAND_PREFIXES):
            return
        region = CHAT_TO_REGION[chat_id]
        if normalized.lower().startswith("/card"):
            # `/card <UPC>` in a group → DM the action card to the operator.
            worker = threading.Thread(
                target=_handle_card_command,
                args=(normalized, message_id, "", sender_open_id, region),
                daemon=True,
            )
            worker.start()
            return
        worker = threading.Thread(target=_handle_command, args=(normalized, message_id, region), daemon=True)
        worker.start()
    except Exception as exc:
        print("im.message.receive error:", repr(exc), flush=True)


def main():
    current_pid = os.getpid()
    write_pid_file(current_pid)
    print(f"Starting Lark persistent connection client for {BOT_APP_ID} from {BOT_SCRIPT} (PID {current_pid})", flush=True)
    print("✓ Reactive JWT refresh enabled (on-demand refresh on rc=4)", flush=True)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(handle_card_action)
        .register_p2_im_message_receive_v1(handle_message_receive)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(handle_bot_p2p_chat_entered)
        .build()
    )
    cli = WsClient(BOT_APP_ID, BOT_SECRET, event_handler=handler)
    cli.start()


if __name__ == "__main__":
    main()
