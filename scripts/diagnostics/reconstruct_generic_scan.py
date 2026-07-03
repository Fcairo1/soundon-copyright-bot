#!/usr/bin/env python3
import json
from pathlib import Path
import os
from datetime import datetime
ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
from scripts.backfill import backfill_ap_direitos_br as br
from copyright_alert import run_alert as ra

parsed_candidates = []
seen_threads = set()
for m in br.iter_triage_messages(ra.TRIAGE_QUERY):
    msg_id = m.get("message_id", "")
    subject = m.get("subject", "")
    date_raw = m.get("date", "")
    thread_id = m.get("thread_id") or msg_id
    msg_dt = br.parse_iso(date_raw)
    if msg_dt and msg_dt < br.CUTOFF:
        break
    reason = br.prefilter_skip_reason(subject, thread_id, seen_threads)
    if reason:
        continue
    seen_threads.add(thread_id)
    body, meta = ra.fetch_email(msg_id)
    if not body:
        continue
    ef = ra.extract_fields(body, subject, meta)
    parsed_candidates.append({
        "message_id": msg_id,
        "thread_id": thread_id,
        "date": date_raw,
        "subject": subject,
        "upc": ef.get("upc"),
        "isrc": ef.get("isrc"),
        "ref_id": ef.get("ref_id"),
    })
print(json.dumps(parsed_candidates, ensure_ascii=False, indent=2))
