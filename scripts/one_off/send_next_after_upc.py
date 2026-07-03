#!/usr/bin/env python3
"""Send one copyright alert for the next applicable case after a given UPC."""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import copyright_alert.run_alert as ra
from copyright_alert.region_guard import assert_region_allowed

AFTER_UPC = "072991770156"
SEARCH_MAX = 250
QUERY = "Infringement Claim"
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


def parse_json(raw):
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except Exception:
                continue
    raise RuntimeError(f"No JSON object in output: {raw[:300]}")


def fetch_email(msg_id):
    cmd = ["lark-cli", "mail", "+message", "--mailbox", ra.MAILBOX, "--message-id", msg_id, "--html=false", "--format", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    parsed = parse_json(res.stdout)
    inner = parsed.get("data", parsed)
    return inner.get("body_plain_text", ""), inner


def recent_candidates():
    cmd = [
        "lark-cli", "mail", "+triage",
        "--mailbox", ra.MAILBOX,
        "--query", QUERY,
        "--max", str(SEARCH_MAX),
        "--format", "json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError((res.stdout + res.stderr)[:1000])
    data = parse_json(res.stdout)
    seen_threads = set()
    out = []
    for m in data.get("messages", []):
        subject = m.get("subject", "")
        if re.match(r"(?i)^(re:|fw:)", subject):
            continue
        if "claim release" in subject.lower():
            continue
        thread_id = m.get("thread_id") or m.get("message_id")
        if thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)
        out.append((m.get("date"), m.get("message_id"), subject))
    return out


def inspect_candidate(date, msg_id, subject):
    body, meta = fetch_email(msg_id)
    if not body:
        return None, "could not fetch email body"
    ef = ra.extract_fields(body, subject, meta)
    upc = ef.get("upc", "")
    isrc = ef.get("isrc", "")
    lookup_id = isrc if isrc and isrc != "N/A" else upc
    lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
    if not lookup_id or lookup_id == "N/A":
        return None, "no ISRC or UPC"
    try:
        ar = ra.query_aeolus(lookup_id, lookup_type)
    except subprocess.TimeoutExpired:
        return None, f"Aeolus query timed out for {lookup_type}:{lookup_id}"
    except Exception as exc:
        return None, f"Aeolus query failed for {lookup_type}:{lookup_id}: {exc}"
    if not ar:
        return None, "no Aeolus data"
    if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
        ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"
    if not qualifies_us_quality(ar):
        return None, f"not applicable: region={ar.get('user_region')} source={ar.get('source_type_name')} tier={ar.get('User Tier')} upc={upc}"
    duplicate_key = ra.claim_key(ef, ar, subject)
    if ra.is_claim_already_posted(duplicate_key):
        return None, f"duplicate claim already posted: {duplicate_key}"
    return (ef, ar, duplicate_key), None


def main():
    configure_us_workflow()
    candidates = recent_candidates()
    after_seen = False
    skipped = []
    for date, msg_id, subject in candidates:
        body, meta = fetch_email(msg_id)
        if not body:
            skipped.append({"date": date, "subject": subject, "reason": "could not fetch for after-upc scan"})
            continue
        ef_quick = ra.extract_fields(body, subject, meta)
        if not after_seen:
            if ef_quick.get("upc") == AFTER_UPC:
                after_seen = True
            continue
        # Reuse fetched data by inlining the same logic to avoid refetching.
        upc = ef_quick.get("upc", "")
        isrc = ef_quick.get("isrc", "")
        lookup_id = isrc if isrc and isrc != "N/A" else upc
        lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
        if not lookup_id or lookup_id == "N/A":
            skipped.append({"date": date, "subject": subject, "reason": "no ISRC or UPC"})
            continue
        try:
            ar = ra.query_aeolus(lookup_id, lookup_type)
        except subprocess.TimeoutExpired:
            skipped.append({"date": date, "subject": subject, "reason": f"Aeolus timeout for {lookup_type}:{lookup_id}"})
            continue
        except Exception as exc:
            skipped.append({"date": date, "subject": subject, "reason": f"Aeolus failed for {lookup_type}:{lookup_id}: {exc}"})
            continue
        if not ar:
            skipped.append({"date": date, "subject": subject, "reason": "no Aeolus data"})
            continue
        if (not ef_quick.get("isrc") or ef_quick.get("isrc") == "N/A") and ar.get("isrc"):
            ef_quick["isrc"] = str(ar.get("isrc")).strip() or "N/A"
        if not qualifies_us_quality(ar):
            skipped.append({"date": date, "subject": subject, "reason": f"not applicable: region={ar.get('user_region')} source={ar.get('source_type_name')} tier={ar.get('User Tier')} upc={upc}"})
            continue
        duplicate_key = ra.claim_key(ef_quick, ar, subject)
        if ra.is_claim_already_posted(duplicate_key):
            skipped.append({"date": date, "subject": subject, "reason": f"duplicate claim already posted: {duplicate_key}"})
            continue
        assert_region_allowed(US_CHAT_ID, ar, upc=ef_quick.get("upc"), context="US group post")

        card = ra.build_card(ef_quick, ar)
        ok, posted_message_id = ra.post_card(card)
        if not ok or not posted_message_id:
            skipped.append({"date": date, "subject": subject, "reason": "card post failed"})
            continue
        card = ra.build_card(ef_quick, ar, lark_message_id=posted_message_id)
        Path("runtime/last_card.json").write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        ra.patch_card_message(posted_message_id, card)
        ra.append_tracker_row(ef_quick, ar, posted_message_id, status="")
        result = {
            "after_upc": AFTER_UPC,
            "date": date,
            "source_message_id": msg_id,
            "subject": subject,
            "posted_message_id": posted_message_id,
            "title": ef_quick.get("title") if ef_quick.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "upc": ef_quick.get("upc", "N/A"),
            "isrc": ef_quick.get("isrc", "N/A"),
            "region": ar.get("user_region", "N/A"),
            "source": ar.get("source_type_name", "N/A"),
            "tier": ar.get("User Tier", "N/A"),
            "chat_id": ra.TARGET_CHAT_ID,
        }
        ra._save_posted_claim(duplicate_key, result)
        out = {"posted": result, "skipped": skipped}
        Path("runtime/next_after_upc_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    out = {"posted": None, "skipped": skipped, "error": f"No applicable unposted case found after UPC {AFTER_UPC}"}
    Path("runtime/next_after_upc_result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
