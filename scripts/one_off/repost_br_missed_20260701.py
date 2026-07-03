#!/usr/bin/env python3
"""Re-post BR daily scan cards missed on 2026-07-01 after stale chat_id fix."""
from __future__ import annotations

from copyright_alert import daily_workflow as dw
from copyright_alert import run_alert as ra
from copyright_alert import bot_runtime as br

MISSED = [
    ("640948294804", 144, "UEFuR0FXeU96VEdJVmd0NzRvL2wxMjV2WHpvPQ==", "Possibly Infringing - Notification Warning No 1 - Lunar Media - Claim"),
    ("046081232374", 146, "bTN5QjVBa0NKM1hsRUxwWE4wbi9YQVYyaUlrPQ==", "Infringement Claim: Spotify - 046081232374 - soundon"),
    ("5063963044431", 145, "SXgzS0hLR255WWIxUWZuV0QxeGI0Y0V3SmlZPQ==", "Possibly Infringing - Notification Warning No 1 - Game Records - Claim"),
]


def main() -> int:
    cfg = dw.configure_region("BR")
    print(f"Using BR chat_id={cfg['chat_id']} ({cfg['chat_name']})")
    aeolus = ra.batch_query_aeolus_by_upc([u for u, _, _, _ in MISSED])
    results = []
    for upc, tracker_row, msg_id, subject in MISSED:
        print(f"\n== Reposting UPC {upc} ==")
        body, meta = ra.fetch_email(msg_id)
        if not body:
            print(f"FAILED {upc}: could not fetch source email {msg_id}")
            results.append((upc, False, ""))
            continue
        ef = ra.extract_fields(body, subject, meta)
        ar = aeolus.get(upc) or {}
        if not ar:
            print(f"FAILED {upc}: no Aeolus data")
            results.append((upc, False, ""))
            continue
        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"
        if not ra.qualifies(ar):
            print(f"FAILED {upc}: no longer qualifies")
            results.append((upc, False, ""))
            continue
        card = ra.build_card(ef, ar)
        ok, posted_id = ra.post_card(card, ar, upc=upc, context="BR missed repost after chat_id fix")
        if not (ok and posted_id):
            print(f"FAILED {upc}: post_card returned ok={ok}, message_id={posted_id!r}")
            results.append((upc, False, ""))
            continue
        card = ra.build_posted_group_card(
            ef,
            ar,
            posted_id,
            source_email_message_id=msg_id,
            tracker_row=tracker_row,
            region="BR",
            ops_dm_email=dw.RECIPIENT_EMAIL,
            ops_dm_open_id=dw.RECIPIENT_OPEN_ID,
            ops_dm_chat_id=dw.RECIPIENT_CHAT_ID,
        )
        ra.patch_card_message(posted_id, card)
        ra.update_tracker_message_ids(tracker_row, posted_id)
        dup_key = ra.claim_key(ef, ar, subject)
        ra._save_posted_claim(dup_key, {
            "message_id": posted_id,
            "source_email_message_id": msg_id,
            "subject": subject,
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "ref_id": ef.get("ref_id", "N/A"),
            "claimant_name": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "chat_id": ra.TARGET_CHAT_ID,
        })
        ra.save_posted_card(posted_id, card)
        print(f"SUCCESS {upc}: {posted_id}")
        results.append((upc, True, posted_id))
    print("\nSUMMARY")
    for upc, ok, posted_id in results:
        print(f"{upc}\t{ok}\t{posted_id}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
