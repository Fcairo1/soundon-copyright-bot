#!/usr/bin/env python3
"""Run the 5 most recent applicable copyright alerts using run_alert.py helpers."""
import json
import re
import subprocess
import sys
from pathlib import Path

# Ensure repo root import path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import copyright_alert.run_alert as ra
from copyright_alert.region_guard import assert_region_allowed

TARGET_COUNT = 5
SEARCH_MAX = 150
QUERY = "Infringement Claim"


def parse_json(raw):
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith('{'):
            try:
                return json.loads('\n'.join(lines[i:]))
            except Exception:
                continue
    raise RuntimeError(f"No JSON object in output: {raw[:300]}")


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
    out = []
    seen_threads = set()
    for m in data.get("messages", []):
        subject = m.get("subject", "")
        # Skip replies/forwards; keep the newest root/original-style alert cases.
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


def fetch_email(msg_id):
    cmd = ["lark-cli", "mail", "+message", "--mailbox", ra.MAILBOX, "--message-id", msg_id, "--html=false", "--format", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    parsed = parse_json(res.stdout)
    inner = parsed.get("data", parsed)
    return inner.get("body_plain_text", ""), inner


def process_one(date, msg_id, subject):
    print("\n" + "=" * 60, flush=True)
    print(f"Processing candidate: {date} | {subject}", flush=True)
    print(f"Message ID: {msg_id}", flush=True)

    body, meta = fetch_email(msg_id)
    if not body:
        return None, "could not fetch email body"
    ef = ra.extract_fields(body, subject, meta)
    upc = ef.get("upc", "")
    isrc = ef.get("isrc", "")
    lookup_id = isrc if isrc and isrc != "N/A" else upc
    lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
    print(f"Extracted UPC={upc} ISRC={isrc}; lookup={lookup_type}:{lookup_id}", flush=True)
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

    if not ra.qualifies(ar):
        return None, f"not applicable: region={ar.get('user_region')} source={ar.get('source_type_name')} tier={ar.get('User Tier')}"

    assert_region_allowed(ra.TARGET_CHAT_ID, ar, upc=ef.get("upc"), context="group post")

    duplicate_key = ra.claim_key(ef, ar, subject)
    if ra.is_claim_already_posted(duplicate_key):
        return None, f"duplicate claim already posted: {duplicate_key}"

    card = ra.build_card(ef, ar)
    ok, posted_message_id = ra.post_card(card)
    if not ok or not posted_message_id:
        return None, "card post failed"
    card = ra.build_card(ef, ar, lark_message_id=posted_message_id)
    Path("copyright_alert/last_card.json").write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    ra.patch_card_message(posted_message_id, card)
    ra.append_tracker_row(ef, ar, posted_message_id, status="")
    result = {
        "date": date,
        "source_message_id": msg_id,
        "subject": subject,
        "posted_message_id": posted_message_id,
        "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
        "artist": ra._format_artist_names(ar.get("display_artist")),
        "upc": ef.get("upc", "N/A"),
        "isrc": ef.get("isrc", "N/A"),
        "region": ar.get("user_region", "N/A"),
        "source": ar.get("source_type_name", "N/A"),
        "tier": ar.get("User Tier", "N/A"),
        "chat_id": ra.TARGET_CHAT_ID,
    }
    ra._save_posted_claim(duplicate_key, result)
    return result, None


def main():
    posted = []
    skipped = []
    candidates = recent_candidates()
    print(f"Found {len(candidates)} root/original candidate cases", flush=True)
    for cand in candidates:
        if len(posted) >= TARGET_COUNT:
            break
        result, reason = process_one(*cand)
        if result:
            posted.append(result)
            print(f"POSTED {len(posted)}/{TARGET_COUNT}: {result['posted_message_id']}", flush=True)
        else:
            skipped.append({"date": cand[0], "message_id": cand[1], "subject": cand[2], "reason": reason})
            print(f"SKIPPED: {reason}", flush=True)
    output = {"posted": posted, "skipped": skipped, "candidate_count": len(candidates)}
    Path("copyright_alert/five_recent_results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nRESULT_JSON_START")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    print("RESULT_JSON_END")
    if len(posted) < TARGET_COUNT:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
