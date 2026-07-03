#!/usr/bin/env python3
import json
import re
from pathlib import Path

from copyright_alert import daily_workflow as dw
from copyright_alert import run_alert as ra
from copyright_alert.region_guard import assert_region_allowed

US_CHAT_ID = "oc_e85373716ee746e3dc1bf999929cf1c4"
US_TRACKER_URL = "https://bytedance.sg.larkoffice.com/sheets/FKqxsTu0bhl3ATt3n7YlIGvfgne"
US_TRACKER_SHEET_ID = "66eefc"
US_REGION_CODES = {"US", "CA", "AU", "NZ"}
US_IGNORED_USERS = {"huang.zeyuan"}


def configure_us_workflow():
    ra.TARGET_CHAT_ID = US_CHAT_ID
    ra.TRACKER_SHEET_URL = US_TRACKER_URL
    ra.TRACKER_SHEET_ID = US_TRACKER_SHEET_ID
    ra.EXCLUDED_MENTIONS.update({user.lower() for user in US_IGNORED_USERS})


def qualifies_us_quality(row):
    region = (row.get("user_region") or "").strip().upper()
    tier = (row.get("User Tier") or "").strip()
    source = (row.get("source_type_name") or "").strip()
    return region in US_REGION_CODES and (source in ("AP", "A&R") or tier == "High Quality")


def is_us_region(row):
    return (row.get("user_region") or "").strip().upper() in US_REGION_CODES


def scan_messages(limit=80):
    dw.TRIAGE_MAX = limit
    messages = dw.fetch_messages_raw()
    seen_threads = set()
    quality_candidate = None
    fallback_candidate = None

    for m in messages:
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        date = m.get("date", "")
        thread_id = m.get("thread_id") or msg_id

        reason = dw._prefilter_skip_reason(subject, thread_id, seen_threads)
        if reason:
            continue
        seen_threads.add(thread_id)
        if not msg_id:
            continue

        body, meta = ra.fetch_email(msg_id)
        if not body:
            continue
        ef = ra.extract_fields(body, subject, meta)
        upc = ef.get("upc", "")
        isrc = ef.get("isrc", "")
        lookup_id = isrc if isrc and isrc != "N/A" else upc
        lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
        if not lookup_id or lookup_id == "N/A":
            continue
        ar = ra.query_aeolus(lookup_id, lookup_type)
        if not ar:
            continue
        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"

        record = {
            "message_id": msg_id,
            "email_date": date,
            "subject": subject,
            "ef": ef,
            "ar": ar,
            "quality_match": qualifies_us_quality(ar),
            "region_match": is_us_region(ar),
        }
        if record["quality_match"]:
            quality_candidate = record
            break
        if record["region_match"] and fallback_candidate is None:
            fallback_candidate = record

    return {"quality_candidate": quality_candidate, "fallback_candidate": fallback_candidate, "fetched": len(messages)}


def post_and_log(candidate):
    configure_us_workflow()

    ef = candidate["ef"]
    ar = candidate["ar"]
    assert_region_allowed(US_CHAT_ID, ar, upc=ef.get("upc"), context="US group post")

    card = ra.build_card(ef, ar)
    post_ok, posted_message_id = ra.post_card(card)
    patch_ok = False
    append_ok = False
    if post_ok and posted_message_id:
        patched = ra.build_card(ef, ar, lark_message_id=posted_message_id)
        patch_ok = ra.patch_card_message(posted_message_id, patched)
        append_ok = ra.append_tracker_row(ef, ar, posted_message_id, status="")
    return {
        "post_ok": post_ok,
        "posted_message_id": posted_message_id,
        "patch_ok": patch_ok,
        "append_ok": append_ok,
    }


def summarize(candidate):
    ef = candidate["ef"]
    ar = candidate["ar"]
    return {
        "upc": ef.get("upc") or ar.get("upc") or "N/A",
        "isrc": ef.get("isrc") or ar.get("isrc") or "N/A",
        "artist": ra._format_artist_names(ar.get("display_artist")),
        "title": ef.get("title") if ef.get("title") and ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
        "claimant": ef.get("claimant_name", "N/A"),
        "claimant_email": ef.get("claimant_email", "N/A"),
        "claim_details": ef.get("claimant_message", "N/A"),
        "dsp": ef.get("dsp", "N/A"),
        "email_source": ef.get("email_source", "N/A"),
        "source_type_name": ar.get("source_type_name", "N/A"),
        "user_tier": ar.get("User Tier", "N/A"),
        "user_region": ar.get("user_region", "N/A"),
        "message_id": candidate.get("message_id", ""),
        "email_date": candidate.get("email_date", ""),
        "subject": candidate.get("subject", ""),
    }


def main():
    scan = scan_messages()
    candidate = scan["quality_candidate"] or scan["fallback_candidate"]
    result = {
        "chat_id": US_CHAT_ID,
        "fetched": scan["fetched"],
        "used_quality_match": bool(scan["quality_candidate"]),
        "found": bool(candidate),
    }
    if not candidate:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    result["case"] = summarize(candidate)
    result["post"] = post_and_log(candidate)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
