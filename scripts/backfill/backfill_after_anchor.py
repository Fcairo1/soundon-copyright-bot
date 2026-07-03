#!/usr/bin/env python3
"""Backfill scan: post BR-qualifying cards received AFTER UPC 5063963471855
(email date 2026-06-16 09:40:54 UTC). Posts to AP Direitos BR
oc_fd2e43d6451f8d87adb4cd4ceefa7816. Updates posted_claims.json,
posted_cards.json, and tracker sheet.
"""
import json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from copyright_alert import run_alert as ra
from copyright_alert.region_guard import assert_region_allowed

# Cutoff = email date of UPC 5063963471855 (the last correctly posted card on Jun 16).
# Process emails STRICTLY NEWER than this timestamp.
CUTOFF = datetime(2026, 6, 16, 9, 40, 55, tzinfo=timezone.utc)
TARGET_CHAT_ID = "oc_fd2e43d6451f8d87adb4cd4ceefa7816"
PAGE_SIZE = 100
MAX_PAGES = 20

ra.TARGET_CHAT_ID = TARGET_CHAT_ID  # ensure the post helper uses correct chat


def parse_iso(ts):
    if not ts: return None
    ts = ts.strip()
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def parse_json_output(raw):
    i = raw.find("{")
    return json.loads(raw[i:]) if i != -1 else None


def iter_triage():
    page_token = None
    page = 0
    while page < MAX_PAGES:
        page += 1
        cmd = ["lark-cli", "mail", "+triage",
               "--mailbox", ra.MAILBOX, "--query", ra.TRIAGE_QUERY,
               "--max", str(PAGE_SIZE), "--format", "json"]
        if page_token:
            cmd.extend(["--page-token", page_token])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if res.returncode != 0:
            raise RuntimeError(f"triage rc={res.returncode}: {(res.stdout+res.stderr)[:600]}")
        parsed = parse_json_output(res.stdout)
        if not parsed: break
        msgs = parsed.get("messages") or (parsed.get("data") or {}).get("messages") or []
        if not msgs: break
        for m in msgs: yield m
        if not parsed.get("has_more") or not parsed.get("page_token"): break
        page_token = parsed.get("page_token")


def prefilter(subject, thread_id, seen):
    s = subject or ""
    if re.match(r"(?i)^(re:|fw:)", s): return "reply/forward"
    if "claim release" in s.lower(): return "claim release"
    if thread_id and thread_id in seen: return f"dup thread"
    return None


def main():
    started = datetime.now(timezone.utc)
    summary = {k: 0 for k in [
        "examined", "skipped_prefilter", "skipped_before_cutoff",
        "skipped_no_upc", "parsed", "skipped_no_aeolus",
        "skipped_not_qualifying", "skipped_already_posted",
        "posted", "tracker_logged", "post_failed"]}
    scanned, posted, skipped = [], [], []
    seen_threads = set()
    candidates = []
    reached_cutoff = False

    for m in iter_triage():
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        date_raw = m.get("date", "")
        thread_id = m.get("thread_id") or msg_id
        msg_dt = parse_iso(date_raw)
        if msg_dt and msg_dt <= CUTOFF:
            reached_cutoff = True
            break
        summary["examined"] += 1
        reason = prefilter(subject, thread_id, seen_threads)
        if reason:
            summary["skipped_prefilter"] += 1
            skipped.append({"upc": "?", "subject": subject[:80], "reason": reason, "date": date_raw})
            continue
        seen_threads.add(thread_id)

        body, meta = ra.fetch_email(msg_id)
        if not body:
            skipped.append({"upc": "?", "subject": subject[:80], "reason": "no body", "date": date_raw})
            continue
        ef = ra.extract_fields(body, subject, meta)
        upc = str(ef.get("upc") or "").strip()
        if not upc or upc == "N/A":
            summary["skipped_no_upc"] += 1
            skipped.append({"upc": "N/A", "subject": subject[:80], "reason": "no upc", "date": date_raw})
            continue
        candidates.append({"msg_id": msg_id, "subject": subject, "date_raw": date_raw, "ef": ef, "upc": upc})

    summary["parsed"] = len(candidates)
    print(f"Candidates parsed (newer than cutoff {CUTOFF.isoformat()}): {len(candidates)}")
    print(f"Reached cutoff: {reached_cutoff}")

    upcs = list({c["upc"] for c in candidates})
    aeolus = ra.batch_query_aeolus_by_upc(upcs, chunk_size=80) if upcs else {}

    prior_claims = ra._load_posted_claims()

    for c in candidates:
        ef = c["ef"]
        upc = c["upc"]
        ar = aeolus.get(upc) or {}
        scan_entry = {
            "upc": upc, "subject": c["subject"][:100], "date": c["date_raw"],
            "ref_id": ef.get("ref_id", "N/A"),
        }
        if not ar:
            summary["skipped_no_aeolus"] += 1
            scan_entry["status"] = "skipped"
            scan_entry["reason"] = "no aeolus row"
            scanned.append(scan_entry)
            skipped.append(scan_entry)
            continue
        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"
        scan_entry["region"] = (ar.get("user_region") or "").strip().upper()
        scan_entry["source"] = ar.get("source_type_name", "")
        scan_entry["tier"] = ar.get("User Tier", "")
        scan_entry["title"] = ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A")
        scan_entry["artist"] = ra._format_artist_names(ar.get("display_artist"))
        if not ra.qualifies(ar):
            summary["skipped_not_qualifying"] += 1
            scan_entry["status"] = "skipped"
            scan_entry["reason"] = f"not qualifying (region={scan_entry['region']},source={scan_entry['source']},tier={scan_entry['tier']})"
            scanned.append(scan_entry)
            skipped.append(scan_entry)
            continue
        dup_key = ra.claim_key(ef, ar, c["subject"])
        if dup_key in prior_claims:
            summary["skipped_already_posted"] += 1
            scan_entry["status"] = "skipped"
            scan_entry["reason"] = f"already posted ({dup_key})"
            scanned.append(scan_entry)
            skipped.append(scan_entry)
            continue

        # Post card
        assert_region_allowed(TARGET_CHAT_ID, ar, upc=upc, context="BR group post")
        card = ra.build_card(ef, ar)
        ok, mid = ra.post_card(card)
        if not (ok and mid):
            summary["post_failed"] += 1
            scan_entry["status"] = "post_failed"
            scanned.append(scan_entry)
            continue
        card = ra.build_card(ef, ar, lark_message_id=mid)
        ra.patch_card_message(mid, card)
        ra.save_posted_card(mid, card)
        ok_tracker = ra.append_tracker_row(ef, ar, mid, status="")
        if ok_tracker:
            summary["tracker_logged"] += 1
        record = {
            "message_id": mid,
            "source_email_message_id": c["msg_id"],
            "subject": c["subject"],
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": scan_entry["title"],
            "artist": scan_entry["artist"],
            "ref_id": ef.get("ref_id", "N/A"),
            "claimant_name": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "date": c["date_raw"],
            "region": scan_entry["region"],
            "source": scan_entry["source"],
            "tier": scan_entry["tier"],
            "chat_id": TARGET_CHAT_ID,
        }
        ra._save_posted_claim(dup_key, record)
        prior_claims[dup_key] = record
        summary["posted"] += 1
        scan_entry["status"] = "posted"
        scan_entry["message_id"] = mid
        scan_entry["tracker_logged"] = ok_tracker
        scanned.append(scan_entry)
        posted.append(scan_entry)

    out = {
        "started": started.isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
        "cutoff": CUTOFF.isoformat(),
        "reached_cutoff": reached_cutoff,
        "summary": summary,
        "scanned": scanned,
        "posted": posted,
        "skipped": skipped,
    }
    Path("runtime/backfill_after_anchor_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("__RESULT__")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
