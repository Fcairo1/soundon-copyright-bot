#!/usr/bin/env python3
import json
from pathlib import Path

from copyright_alert import daily_workflow as dw
from copyright_alert import run_alert as ra

US_REGION_CODES = {"US", "CA", "AU", "NZ"}
TARGET_AFTER_UPC = "072991770156"
MAX_MESSAGES = 220
OUTPUT = Path("copyright_alert/us_candidate_scan.json")


def qualifies_us_quality(row):
    region = (row.get("user_region") or "").strip().upper()
    tier = (row.get("User Tier") or "").strip()
    source = (row.get("source_type_name") or "").strip()
    return region in US_REGION_CODES and (source in ("AP", "A&R") or tier == "High Quality")


def main():
    dw.TRIAGE_MAX = MAX_MESSAGES
    messages = dw.fetch_messages_raw()
    seen_threads = set()
    after_seen = False
    scanned = []
    posted = ra._load_posted_claims()
    for m in messages:
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        date = m.get("date", "")
        thread_id = m.get("thread_id") or msg_id
        reason = dw._prefilter_skip_reason(subject, thread_id, seen_threads)
        if reason:
            scanned.append({"date": date, "message_id": msg_id, "subject": subject, "stage": "prefilter", "reason": reason})
            continue
        seen_threads.add(thread_id)
        if not msg_id:
            scanned.append({"date": date, "message_id": msg_id, "subject": subject, "stage": "prefilter", "reason": "missing message_id"})
            continue
        body, meta = ra.fetch_email(msg_id)
        if not body:
            scanned.append({"date": date, "message_id": msg_id, "subject": subject, "stage": "fetch", "reason": "no email body"})
            continue
        ef = ra.extract_fields(body, subject, meta)
        upc = ef.get("upc", "")
        if not after_seen:
            if upc == TARGET_AFTER_UPC:
                after_seen = True
                scanned.append({"date": date, "message_id": msg_id, "subject": subject, "upc": upc, "stage": "anchor", "reason": "found after_upc anchor"})
            else:
                scanned.append({"date": date, "message_id": msg_id, "subject": subject, "upc": upc, "stage": "before_anchor", "reason": "before target UPC"})
            continue
        isrc = ef.get("isrc", "")
        lookup_id = isrc if isrc and isrc != "N/A" else upc
        lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
        if not lookup_id or lookup_id == "N/A":
            scanned.append({"date": date, "message_id": msg_id, "subject": subject, "upc": upc, "stage": "lookup", "reason": "no ISRC or UPC"})
            continue
        ar = ra.query_aeolus(lookup_id, lookup_type)
        if not ar:
            scanned.append({"date": date, "message_id": msg_id, "subject": subject, "upc": upc, "lookup_type": lookup_type, "lookup_id": lookup_id, "stage": "aeolus", "reason": "no Aeolus data"})
            continue
        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"
        region = (ar.get("user_region") or "").strip().upper()
        source = (ar.get("source_type_name") or "").strip()
        tier = (ar.get("User Tier") or "").strip()
        claim_key = ra.claim_key(ef, ar, subject)
        is_posted = bool(claim_key and claim_key in posted)
        row = {
            "date": date,
            "message_id": msg_id,
            "subject": subject,
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "region": region,
            "source": source,
            "tier": tier,
            "claim_key": claim_key,
            "posted": is_posted,
            "title": ef.get("title") if ef.get("title") and ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "claimant": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "dsp": ef.get("dsp", "N/A"),
        }
        if region not in US_REGION_CODES:
            row.update({"stage": "region", "reason": "outside US region set"})
            scanned.append(row)
            continue
        if not qualifies_us_quality(ar):
            row.update({"stage": "quality", "reason": "region matched but source/tier did not qualify"})
            scanned.append(row)
            continue
        if is_posted:
            row.update({"stage": "duplicate", "reason": "already posted"})
            scanned.append(row)
            continue
        row.update({"stage": "selected", "reason": "next applicable unposted case"})
        result = {"found": True, "after_seen": after_seen, "candidate": row, "scanned": scanned}
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    result = {"found": False, "after_seen": after_seen, "candidate": None, "scanned": scanned}
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
