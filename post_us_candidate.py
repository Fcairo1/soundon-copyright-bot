#!/usr/bin/env python3
import json
from pathlib import Path

from copyright_alert import run_alert as ra
from copyright_alert.region_guard import assert_region_allowed

US_CHAT_ID = "oc_e85373716ee746e3dc1bf999929cf1c4"
US_TRACKER_URL = "https://bytedance.sg.larkoffice.com/sheets/FKqxsTu0bhl3ATt3n7YlIGvfgne"
US_TRACKER_SHEET_ID = "66eefc"
US_IGNORED_USERS = {"huang.zeyuan"}
SOURCE_FILE = Path("copyright_alert/us_next_after_anchor.json")
OUTPUT_FILE = Path("copyright_alert/us_post_result.json")


def configure_us_workflow():
    ra.TARGET_CHAT_ID = US_CHAT_ID
    ra.TRACKER_SHEET_URL = US_TRACKER_URL
    ra.TRACKER_SHEET_ID = US_TRACKER_SHEET_ID
    ra.EXCLUDED_MENTIONS.update({user.lower() for user in US_IGNORED_USERS})


def main():
    configure_us_workflow()
    payload = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    candidate = payload.get("candidate") or {}
    if not candidate:
        raise SystemExit("No candidate found in us_next_after_anchor.json")
    msg_id = candidate["message_id"]
    subject = candidate["subject"]
    body, meta = ra.fetch_email(msg_id)
    ef = ra.extract_fields(body, subject, meta)
    upc = ef.get("upc", "")
    isrc = ef.get("isrc", "")
    lookup_id = isrc if isrc and isrc != "N/A" else upc
    lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
    ar = ra.query_aeolus(lookup_id, lookup_type)
    if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
        ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"

    duplicate_key = ra.claim_key(ef, ar, subject)
    if ra.is_claim_already_posted(duplicate_key):
        result = {
            "skipped": True,
            "reason": "duplicate claim already posted",
            "claim_key": duplicate_key,
            "candidate": candidate,
        }
        OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    assert_region_allowed(US_CHAT_ID, ar, upc=ef.get("upc"), context="US group post")

    card = ra.build_card(ef, ar)
    post_ok, posted_message_id = ra.post_card(card)
    patch_ok = False
    append_ok = False
    if post_ok and posted_message_id:
        patched = ra.build_card(ef, ar, lark_message_id=posted_message_id)
        patch_ok = ra.patch_card_message(posted_message_id, patched)
        append_ok = ra.append_tracker_row(ef, ar, posted_message_id, status="")
        ra._save_posted_claim(duplicate_key, {
            "message_id": posted_message_id,
            "source_email_message_id": msg_id,
            "subject": subject,
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "ref_id": ef.get("ref_id", "N/A"),
            "region": ar.get("user_region", "N/A"),
            "source": ar.get("source_type_name", "N/A"),
            "tier": ar.get("User Tier", "N/A"),
            "date": candidate.get("date", ""),
            "chat_id": ra.TARGET_CHAT_ID,
          })
    result = {
        "skipped": False,
        "claim_key": duplicate_key,
        "candidate": candidate,
        "ef": ef,
        "ar": ar,
        "post_ok": post_ok,
        "posted_message_id": posted_message_id,
        "patch_ok": patch_ok,
        "append_ok": append_ok,
    }
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
