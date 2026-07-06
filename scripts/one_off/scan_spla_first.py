#!/usr/bin/env python3
"""scan_spla_first.py — scan further back in soundon-copyright inbox to find
the first SPLA-qualifying infringement claim, post the alert card to the SPLA
group chat, and DM the action card to the SPLA Ops owner (bernardo.sanchez).

Reuses run_alert / daily_workflow / dm_action_card helpers already configured
for SPLA via bot_runtime.configure_region.
"""
import json
import os
import sys
from datetime import datetime, date
from pathlib import Path

# Make sure the package is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copyright_alert import bot_runtime, run_alert as ra, daily_workflow as dw
from copyright_alert.dm_action_card import send_dm_action_card
from copyright_alert.region_guard import assert_region_allowed

REGION = "SPLA"
TRIAGE_MAX = 400                # << go much further back than the prior 50
OUTPUT = Path(f"copyright_alert/spla_scan_result_{int(datetime.utcnow().timestamp())}.json")


def main():
    cfg = bot_runtime.configure_region(REGION)
    dw.TRIAGE_MAX = TRIAGE_MAX
    ra.TRIAGE_MAX = TRIAGE_MAX
    print(f"== SPLA scan, fetching up to {TRIAGE_MAX} emails ==")
    print(f"   chat_id={cfg['chat_id']}  ops_dm_open_id={cfg.get('ops_dm_open_id')}  countries={sorted(cfg['countries'])}")

    # Fetch newest-first.
    messages = dw.fetch_messages_raw()
    print(f"Fetched {len(messages)} messages")

    seen_threads = set()
    posted = ra._load_posted_claims()
    scanned_summary = []
    selected = None

    for m in messages:
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        date_str = m.get("date", "")
        thread_id = m.get("thread_id") or msg_id

        reason = dw._prefilter_skip_reason(subject, thread_id, seen_threads)
        if reason:
            scanned_summary.append({"date": date_str, "subject": subject[:90], "stage": "prefilter", "reason": reason})
            continue
        if not msg_id:
            continue
        seen_threads.add(thread_id)

        body, meta = ra.fetch_email(msg_id)
        if not body:
            scanned_summary.append({"date": date_str, "subject": subject[:90], "stage": "fetch", "reason": "no body"})
            continue

        ef = ra.extract_fields(body, subject, meta)
        upc = (ef.get("upc") or "").strip()
        isrc = (ef.get("isrc") or "").strip()
        if not upc or upc == "N/A":
            scanned_summary.append({"date": date_str, "subject": subject[:90], "stage": "extract", "reason": "no UPC"})
            continue

        lookup_id = isrc if isrc and isrc != "N/A" else upc
        lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
        ar = ra.query_aeolus(lookup_id, lookup_type)
        if not ar:
            scanned_summary.append({"date": date_str, "subject": subject[:90], "upc": upc, "stage": "aeolus", "reason": "no row"})
            continue

        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"

        region_val = (ar.get("user_region") or "").strip().upper()
        scanned_summary.append({
            "date": date_str, "subject": subject[:90], "upc": upc, "region": region_val,
            "source": ar.get("source_type_name"), "tier": ar.get("User Tier"),
            "stage": "aeolus_ok",
        })
        if region_val not in cfg["countries"]:
            continue
        if not ra.qualifies(ar):
            continue
        dup_key = ra.claim_key(ef, ar, subject)
        if dup_key in posted:
            scanned_summary[-1]["reason"] = "already posted"
            continue

        print(f"\n>>> SPLA candidate: UPC={upc} region={region_val} subject={subject[:80]}")
        assert_region_allowed(ra.TARGET_CHAT_ID, ar, upc=upc, context="SPLA group post")
        # Post the group card
        card = ra.build_card(ef, ar)
        with open("copyright_alert/last_card.json", "w") as f:
            json.dump(card, f, indent=2)
        ok, posted_msg_id = ra.post_card(card)
        print(f"  post_card -> ok={ok} msg_id={posted_msg_id}")
        if not ok or not posted_msg_id:
            scanned_summary[-1]["reason"] = "post failed"
            continue

        card = ra.build_card(ef, ar, lark_message_id=posted_msg_id)
        ra.patch_card_message(posted_msg_id, card)

        try:
            ra.append_tracker_row(ef, ar, posted_msg_id, status="")
        except Exception as exc:
            print(f"  ⚠ tracker append failed: {exc!r}")

        ra._save_posted_claim(dup_key, {
            "message_id": posted_msg_id,
            "source_email_message_id": msg_id,
            "subject": subject,
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "ref_id": ef.get("ref_id", "N/A"),
            "claimant_name": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "region": REGION,
            "date": date_str,
            "chat_id": ra.TARGET_CHAT_ID,
        })

        # DM action card to bernardo
        case = {
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "claimant_name": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "source_email_message_id": msg_id,
            "lark_card_message_id": posted_msg_id,
            "detected_at": ef.get("date_received") or date_str,
            "tracker_row": "",
            "ref_id": ef.get("ref_id", ""),
            "region": REGION,
            "ops_dm_email": cfg.get("ops_dm_email", ""),
            "ops_dm_open_id": cfg.get("ops_dm_open_id", ""),
            "ops_dm_chat_id": cfg.get("ops_dm_chat_id", ""),
        }
        dm_ok = False
        try:
            dm_ok = send_dm_action_card(case)
        except Exception as exc:
            print(f"  ✗ DM send error: {exc!r}")
        print(f"  send_dm_action_card -> {dm_ok}")

        selected = {
            "message_id": msg_id,
            "subject": subject,
            "date": date_str,
            "upc": upc,
            "isrc": ef.get("isrc"),
            "region": region_val,
            "source": ar.get("source_type_name"),
            "tier": ar.get("User Tier"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "claimant_name": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "posted_message_id": posted_msg_id,
            "dm_sent": dm_ok,
        }
        break

    result = {
        "scanned": len(scanned_summary),
        "fetched": len(messages),
        "selected": selected,
        "details": scanned_summary,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== Done. Scanned {len(scanned_summary)} (fetched {len(messages)}). Output: {OUTPUT} ==")
    if selected:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        print("No SPLA-qualifying case found.")


if __name__ == "__main__":
    main()
