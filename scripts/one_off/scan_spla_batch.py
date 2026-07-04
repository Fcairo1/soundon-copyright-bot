#!/usr/bin/env python3
"""scan_spla_batch.py — fast batch SPLA scanner.

Strategy: fetch up to 400 newest emails, parallel-extract UPCs from bodies,
then query Aeolus in batches of 40 UPCs to find SPLA-qualifying rows.
Once a SPLA candidate is found (newest-first), post the alert card and DM
bernardo. Designed to be much faster than the per-email run_alert flow.
"""
import json
import os
import sys
import re
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copyright_alert import bot_runtime, run_alert as ra, daily_workflow as dw
from copyright_alert.dm_action_card import send_dm_action_card
from copyright_alert.region_guard import assert_region_allowed

REGION = "SPLA"
TRIAGE_MAX = 400
PARALLEL_FETCH = 8
OUTPUT = Path(f"copyright_alert/spla_batch_result_{int(datetime.utcnow().timestamp())}.json")
LOG = Path("copyright_alert/spla_batch.log")


def log(msg):
    print(msg, flush=True)
    LOG.open("a").write(msg + "\n")


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
    return {
        "msg_id": msg_id, "subject": subject, "date": date_str,
        "ef": ef, "upc": upc,
    }


def main():
    LOG.write_text("")
    cfg = bot_runtime.configure_region(REGION)
    dw.TRIAGE_MAX = TRIAGE_MAX
    log(f"== SPLA batch scan, max={TRIAGE_MAX}, parallel_fetch={PARALLEL_FETCH} ==")
    log(f"   chat_id={cfg['chat_id']}  ops_dm_open_id={cfg.get('ops_dm_open_id')}  countries={sorted(cfg['countries'])}")

    messages = dw.fetch_messages_raw()
    log(f"Fetched {len(messages)} messages from inbox")

    # Pre-filter (subject + thread-dedupe) preserving order.
    seen_threads = set()
    filtered = []
    skipped_prefilter = 0
    for m in messages:
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        thread_id = m.get("thread_id") or msg_id
        reason = dw._prefilter_skip_reason(subject, thread_id, seen_threads)
        if reason or not msg_id:
            skipped_prefilter += 1
            continue
        seen_threads.add(thread_id)
        filtered.append(m)
    log(f"After prefilter: {len(filtered)} (skipped {skipped_prefilter})")

    # Parallel email body fetch + UPC extraction.
    parsed = []
    with ThreadPoolExecutor(max_workers=PARALLEL_FETCH) as pool:
        futures = {pool.submit(fetch_one, m): i for i, m in enumerate(filtered)}
        done = 0
        results_by_idx = {}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = None
            results_by_idx[i] = r
            done += 1
            if done % 40 == 0:
                log(f"  body-fetch progress: {done}/{len(filtered)}")
        # Preserve newest-first order.
        for i in range(len(filtered)):
            r = results_by_idx.get(i)
            if r:
                parsed.append(r)
    log(f"Parsed {len(parsed)} emails with UPC extracted (newest-first preserved)")

    # Batch Aeolus by unique UPC.
    upcs = []
    seen_upc = set()
    for p in parsed:
        if p["upc"] not in seen_upc:
            seen_upc.add(p["upc"])
            upcs.append(p["upc"])
    log(f"Querying Aeolus for {len(upcs)} unique UPCs (batches of 40)...")
    aeolus_by_upc = ra.batch_query_aeolus_by_upc(upcs)
    log(f"Aeolus returned data for {len(aeolus_by_upc)}/{len(upcs)} UPCs")

    posted = ra._load_posted_claims()
    selected = None
    summary_rows = []
    for p in parsed:
        upc = p["upc"]
        ar = aeolus_by_upc.get(upc) or {}
        if not ar:
            summary_rows.append({"upc": upc, "stage": "no_aeolus", "subject": p["subject"][:80], "date": p["date"]})
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
            summary_rows.append(row)
            continue
        if not (source in ("AP", "A&R") or tier == "High Quality"):
            row["stage"] = "SPLA_but_low_quality"
            summary_rows.append(row)
            continue
        dup_key = ra.claim_key(ef, ar, p["subject"])
        if dup_key in posted:
            row["stage"] = "already_posted"
            summary_rows.append(row)
            continue
        row["stage"] = "selected"
        summary_rows.append(row)
        log(f"\n>>> SPLA candidate found: UPC={upc} region={region_val} source={source} tier={tier}")
        log(f"    subject={p['subject']}  date={p['date']}")
        assert_region_allowed(ra.TARGET_CHAT_ID, ar, upc=upc, context="SPLA group post")

        # Post alert card.
        card = ra.build_card(ef, ar)
        with open("copyright_alert/last_card.json", "w") as f:
            json.dump(card, f, indent=2)
        ok, posted_msg_id = ra.post_card(card)
        log(f"   post_card ok={ok} msg_id={posted_msg_id}")
        if not ok or not posted_msg_id:
            row["stage"] = "post_failed"
            continue
        card = ra.build_card(ef, ar, lark_message_id=posted_msg_id)
        ra.patch_card_message(posted_msg_id, card)
        try:
            ra.append_tracker_row(ef, ar, posted_msg_id, status="")
        except Exception as exc:
            log(f"   ⚠ tracker append failed: {exc!r}")

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
            log(f"   ✗ DM error: {exc!r}")
        log(f"   send_dm_action_card -> {dm_ok}")

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
        break

    result = {
        "fetched": len(messages),
        "after_prefilter": len(filtered),
        "parsed_with_upc": len(parsed),
        "unique_upcs": len(upcs),
        "selected": selected,
        "rows_by_stage": summary_rows,
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n== Done. Output: {OUTPUT} ==")
    if selected:
        log(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        log("No SPLA-qualifying case found in scanned batch.")


if __name__ == "__main__":
    main()
