#!/usr/bin/env python3
import json
from copyright_alert import run_alert as ra
from copyright_alert.region_guard import assert_region_allowed

US_CHAT_ID = "oc_e85373716ee746e3dc1bf999929cf1c4"
US_TRACKER_URL = "https://bytedance.sg.larkoffice.com/sheets/FKqxsTu0bhl3ATt3n7YlIGvfgne"
US_TRACKER_SHEET_ID = "66eefc"
US_IGNORED_USERS = {"huang.zeyuan"}
MESSAGE_ID = "WGNDeVg0SVhJWjJudFQwNXUyOGROY2lrdVVFPQ=="


def configure_us_workflow():
    ra.TARGET_CHAT_ID = US_CHAT_ID
    ra.TRACKER_SHEET_URL = US_TRACKER_URL
    ra.TRACKER_SHEET_ID = US_TRACKER_SHEET_ID
    ra.EXCLUDED_MENTIONS.update({user.lower() for user in US_IGNORED_USERS})


def main():
    configure_us_workflow()
    body, meta = ra.fetch_email(MESSAGE_ID)
    ef = ra.extract_fields(body, meta.get("subject", ""), meta)
    ar = ra.query_aeolus(ef.get("upc", ""), "upc")

    assert_region_allowed(US_CHAT_ID, ar, upc=ef.get("upc"), context="US group post")

    card = ra.build_card(ef, ar)
    post_ok, posted_message_id = ra.post_card(card)
    patch_ok = False
    append_ok = False
    if post_ok and posted_message_id:
        patched = ra.build_card(ef, ar, lark_message_id=posted_message_id)
        patch_ok = ra.patch_card_message(posted_message_id, patched)
        append_ok = ra.append_tracker_row(ef, ar, posted_message_id, status="")

    print(json.dumps({
        "chat_id": US_CHAT_ID,
        "message_id": MESSAGE_ID,
        "ef": ef,
        "ar": ar,
        "post_ok": post_ok,
        "posted_message_id": posted_message_id,
        "patch_ok": patch_ok,
        "append_ok": append_ok,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
