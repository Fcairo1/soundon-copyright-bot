#!/usr/bin/env python3
"""scan_spla_paginated.py — paginate through soundon-copyright inbox in
batches of 400 to find a SPLA-qualifying case (going back as far as needed).

For each batch, parse + batch-Aeolus and stop on first SPLA match.
"""
import json
import os
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copyright_alert import bot_runtime, run_alert as ra, daily_workflow as dw
from copyright_alert.dm_action_card import send_dm_action_card
from copyright_alert.region_guard import assert_region_allowed

REGION = "SPLA"
PAGE_SIZE = 400
MAX_PAGES = 6           # up to ~2400 emails total
PARALLEL_FETCH = 10
OUTPUT = Path(f"runtime/spla_paginated_result_{int(datetime.utcnow().timestamp())}.json")


def fetch_page(query, mailbox, page_size, page_token=None):
    cmd = [
        "lark-cli", "mail", "+triage",
        "--mailbox", mailbox,
        "--query", query,
        "--max", str(page_size),
        "--format", "json",
    ]
    if page_token:
        cmd += ["--page-token", page_token]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        print(f"  triage failed rc={res.returncode}: {(res.stdout+res.stderr)[:300]}")
        return [], None
    parsed = ra.parse_lark_json(res.stdout)
    if not parsed:
        return [], None
    msgs = parsed.get("messages") or (parsed.get("data") or {}).get("messages") or []
    nxt = parsed.get("page_token") or (parsed.get("data") or {}).get("page_token")
    return msgs, nxt


def fetch_one(m):
    msg_id = m.get("message_id", "")
    subject = m.get("subject", "")
    date_str = m.get("date", "")
    if not msg_id:
        return None
    body, meta = ra.fetch_email(msg_id)
    if not body:
        return None
    ef = ra.extract_fields(body, subject, meta)
    upc = (ef.get("upc") or "").strip()
    if not upc or upc == "N/A":
        return None
    return {"msg_id": msg_id, "subject": subject, "date": date_str, "ef": ef, "upc": upc}


def process_batch(messages, cfg, posted, scanned_summary, total_scanned):
    seen_threads = set()
    filtered = []
    for m in messages:
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        thread_id = m.get("thread_id") or msg_id
        reason = dw._prefilter_skip_reason(subject, thread_id, seen_threads)
        if reason or not msg_id:
            continue
        seen_threads.add(thread_id)
        filtered.append(m)
    print(f"  prefilter: kept {len(filtered)}/{len(messages)}")

    parsed = []
    with ThreadPoolExecutor(max_workers=PARALLEL_FETCH) as pool:
        futures = {pool.submit(fetch_one, m): i for i, m in enumerate(filtered)}
        results = {}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = None
        for i in range(len(filtered)):
            if results.get(i):
                parsed.append(results[i])
    print(f"  parsed UPCs: {len(parsed)}")

    upcs, seen_upc = [], set()
    for p in parsed:
        if p["upc"] not in seen_upc:
            seen_upc.add(p["upc"])
            upcs.append(p["upc"])
    aeolus_by_upc = ra.batch_query_aeolus_by_upc(upcs) if upcs else {}
    print(f"  aeolus rows: {len(aeolus_by_upc)}/{len(upcs)}")

    selected = None
    for p in parsed:
        upc = p["upc"]
        ar = aeolus_by_upc.get(upc) or {}
        if not ar:
            scanned_summary.append({"upc": upc, "stage": "no_aeolus", "subject": p["subject"][:80], "date": p["date"]})
            continue
        ef = p["ef"]
        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"
        region_val = (ar.get("user_region") or "").strip().upper()
        source = (ar.get("source_type_name") or "").strip()
        tier = (ar.get("User Tier") or "").strip()
        row = {"upc": upc, "region": region_val, "source": source, "tier": tier,
               "subject": p["subject"][:80], "date": p["date"]}
        if region_val not in cfg["countries"]:
            row["stage"] = "outside_SPLA"
            scanned_summary.append(row); continue
        if not (source in ("AP", "A&R") or tier == "High Quality"):
            row["stage"] = "SPLA_but_low_quality"
            scanned_summary.append(row); continue
        dup_key = ra.claim_key(ef, ar, p["subject"])
        if dup_key in posted:
            row["stage"] = "already_posted"
            scanned_summary.append(row); continue
        row["stage"] = "selected"
        scanned_summary.append(row)
        print(f"\n>>> SPLA candidate: UPC={upc} region={region_val} source={source} tier={tier}")
        print(f"    subject={p['subject']}  date={p['date']}")
        assert_region_allowed(ra.TARGET_CHAT_ID, ar, upc=upc, context="SPLA group post")

        card = ra.build_card(ef, ar)
        with open("runtime/last_card.json", "w") as f:
            json.dump(card, f, indent=2)
        ok, posted_msg_id = ra.post_card(card)
        print(f"    post_card ok={ok} msg_id={posted_msg_id}")
        if not ok or not posted_msg_id:
            continue
        card = ra.build_card(ef, ar, lark_message_id=posted_msg_id)
        ra.patch_card_message(posted_msg_id, card)
        try:
            ra.append_tracker_row(ef, ar, posted_msg_id, status="")
        except Exception as exc:
            print(f"    ⚠ tracker append failed: {exc!r}")

        ra._save_posted_claim(dup_key, {
            "message_id": posted_msg_id,
            "source_email_message_id": p["msg_id"],
            "subject": p["subject"],
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "ref_id": ef.get("ref_id", "N/A"),
            "claimant_name": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "region": REGION,
            "date": p["date"],
            "chat_id": ra.TARGET_CHAT_ID,
        })
        case = {
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "claimant_name": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "source_email_message_id": p["msg_id"],
            "lark_card_message_id": posted_msg_id,
            "detected_at": ef.get("date_received") or p["date"],
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
            print(f"    DM error: {exc!r}")
        print(f"    DM sent: {dm_ok}")
        selected = {
            "message_id": p["msg_id"], "subject": p["subject"], "date": p["date"],
            "upc": upc, "isrc": ef.get("isrc"), "region": region_val,
            "source": source, "tier": tier,
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "claimant_name": ef.get("claimant_name", "N/A"),
            "claimant_email": ef.get("claimant_email", "N/A"),
            "posted_message_id": posted_msg_id,
            "dm_sent": dm_ok,
        }
        return selected
    return None


def main():
    cfg = bot_runtime.configure_region(REGION)
    posted = ra._load_posted_claims()
    scanned_summary = []
    total_fetched = 0
    page_token = None
    selected = None
    page_idx = 0
    pages_data = []
    while page_idx < MAX_PAGES:
        page_idx += 1
        print(f"\n=== Page {page_idx} (page_token={page_token}) ===")
        msgs, nxt = fetch_page("Infringement Claim", "soundon-copyright@bytedance.com", PAGE_SIZE, page_token)
        print(f"  fetched {len(msgs)} messages")
        total_fetched += len(msgs)
        if not msgs:
            break
        sel = process_batch(msgs, cfg, posted, scanned_summary, total_fetched)
        pages_data.append({"page": page_idx, "fetched": len(msgs), "next_token": nxt})
        if sel:
            selected = sel
            break
        if not nxt:
            print("  no next page_token — reached end of mailbox")
            break
        page_token = nxt

    result = {
        "pages": pages_data,
        "total_fetched": total_fetched,
        "scanned_summary_count": len(scanned_summary),
        "selected": selected,
        "rows_by_stage": scanned_summary,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== Done. Total fetched: {total_fetched}. Output: {OUTPUT} ==")
    if selected:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        print("No SPLA-qualifying case found across all scanned pages.")


if __name__ == "__main__":
    main()
