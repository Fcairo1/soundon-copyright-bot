#!/usr/bin/env python3
"""One-off TEST harness: find the most recent BR claim in the inbox and send a
DM action card to filipe.cairo. Sends NO email (TEST_MODE handles that on click).
"""
import json, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from copyright_alert.run_alert import (
    fetch_recent_candidates, fetch_email, extract_fields, batch_query_aeolus_by_upc,
    qualifies, parse_lark_json, _format_artist_names,
)
from copyright_alert import dm_action_card

def main():
    cands = fetch_recent_candidates()
    print(f"Triage returned {len(cands)} candidates")

    # Phase 1: cheap pass — fetch each email, keep those with UPC + claimant email.
    rows = []  # (mid, subj, ef)
    for mid, subj in cands:
        if not mid:
            continue
        body, meta = fetch_email(mid)
        if not body:
            continue
        ef = extract_fields(body, subj, meta)
        upc = ef.get("upc")
        cemail = ef.get("claimant_email")
        if not upc or upc == "N/A":
            continue
        if not cemail or cemail == "N/A":
            continue
        rows.append((mid, subj, ef))
    print(f"Pre-filtered to {len(rows)} candidates with UPC + claimant email")

    # Phase 2: single batched Aeolus lookup for all UPCs.
    upcs = list({r[2].get("upc") for r in rows})
    ar_map = batch_query_aeolus_by_upc(upcs) or {}

    # Phase 3: candidates are newest-first; pick the first that qualifies (BR).
    chosen = None
    for mid, subj, ef in rows:
        ar = ar_map.get(ef.get("upc")) or {}
        if not ar:
            continue
        if not qualifies(ar):
            continue
        chosen = (mid, subj, ef, ar)
        break
    if not chosen:
        print("No qualifying BR case with UPC + claimant email found.")
        return
    mid, subj, ef, ar = chosen
    artist = _format_artist_names(ar.get("display_artist"))
    title = ef.get("title") if ef.get("title")!="N/A" else ar.get("album_title","N/A")
    case = {
        "upc": ef.get("upc","N/A"),
        "isrc": ef.get("isrc","N/A") if ef.get("isrc","N/A")!="N/A" else ar.get("isrc","N/A"),
        "title": title or "N/A",
        "artist": artist or "N/A",
        "claimant_name": ef.get("claimant_name","N/A"),
        "claimant_email": ef.get("claimant_email","N/A"),
        "source_email_message_id": mid,
        "lark_card_message_id": "",          # test: no group card linked
        "detected_at": ef.get("date_received","N/A"),
        "tracker_row": None,                  # test: not writing to sheet
    }
    print("CHOSEN_CASE="+json.dumps({k:case[k] for k in ("upc","isrc","title","artist","claimant_name","claimant_email","detected_at","source_email_message_id")}, ensure_ascii=False))
    ok = dm_action_card.send_dm_action_card(case)
    print("DM_SEND_OK="+str(ok))

if __name__=="__main__":
    main()
