#!/usr/bin/env python3
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import os
os.chdir(ROOT)

from copyright_alert import run_alert as ra
from scripts.backfill import backfill_ap_direitos_br as br

CUTOFF = br.CUTOFF
TARGET_UPCS = [
    "073011433457", "5063964388275", "5063964668681", "073011892957", "790566637399",
    "010735103334", "672079828238", "047752485006", "038557586685", "638022968109",
    "5063964654974", "5063964187809", "5063964710274", "5063964638790", "5063964795035",
    "049362112921", "052599254098", "601294340430", "074417691533", "682106738423",
    "796728442269", "074843398594", "047752352704", "5063964471855", "5063936513445",
]
KNOWN_POSTED_SKIP_EXPLAIN = {
    "073011433457", "5063964654974", "5063964710274", "074843398594", "047752352704"
}
TARGETS = [u for u in TARGET_UPCS if u not in KNOWN_POSTED_SKIP_EXPLAIN]


def parse_iso(ts: str):
    if not ts:
        return None
    ts = ts.strip()
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def load_json(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


posted_default = load_json("runtime/posted_claims.json")
posted_br = load_json("runtime/posted_claims_ap_direitos_br.json")
posted_records = {"posted_claims.json": posted_default, "posted_claims_ap_direitos_br.json": posted_br}


def find_dedup_records(upc):
    hits = []
    for name, data in posted_records.items():
        for key, rec in (data or {}).items():
            if isinstance(rec, dict) and str(rec.get("upc", "")).strip() == upc:
                hits.append({
                    "file": name,
                    "claim_key": key,
                    "message_id": rec.get("message_id"),
                    "subject": rec.get("subject"),
                    "date": rec.get("date"),
                    "chat_id": rec.get("chat_id"),
                })
    return hits


# Query Aeolus once for all UPCs under investigation.
aeolus_rows = ra.batch_query_aeolus_by_upc(TARGETS, chunk_size=80)


results = []
for upc in TARGETS:
    triage_matches = []
    page_token = None
    page = 0
    while page < 10:
        page += 1
        cmd = [
            "lark-cli", "mail", "+triage",
            "--mailbox", ra.MAILBOX,
            "--query", upc,
            "--max", "100",
            "--format", "json",
        ]
        if page_token:
            cmd.extend(["--page-token", page_token])
        res = br.run_cmd(cmd, timeout=240)
        parsed = br.parse_json_output(res.stdout) if res.returncode == 0 else None
        if not parsed:
            break
        messages = parsed.get("messages") or (parsed.get("data") or {}).get("messages") or []
        if not messages:
            break
        triage_matches.extend(messages)
        has_more = bool(parsed.get("has_more"))
        page_token = parsed.get("page_token")
        if not has_more or not page_token:
            break

    detailed = []
    for m in triage_matches:
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        thread_id = m.get("thread_id") or msg_id
        dt = parse_iso(m.get("date", ""))
        body, meta = ra.fetch_email(msg_id)
        ef = ra.extract_fields(body, subject, meta) if body else {}
        extracted_upc = str((ef or {}).get("upc", "") or "").strip()
        prefilter_reason = br.prefilter_skip_reason(subject, thread_id, set())
        detailed.append({
            "message_id": msg_id,
            "thread_id": thread_id,
            "date": m.get("date"),
            "date_formatted": (dt.isoformat() if dt else None),
            "subject": subject,
            "prefilter_reason": prefilter_reason,
            "body_found": bool(body),
            "extracted_upc": extracted_upc or None,
            "extracted_isrc": (ef or {}).get("isrc"),
            "ref_id": (ef or {}).get("ref_id"),
            "claim_id_from_subject": ra.first(subject or "", r"Claim\s+(\d{6,})"),
            "after_cutoff": bool(dt and dt >= CUTOFF),
        })

    after_cutoff = [d for d in detailed if d.get("after_cutoff")]
    before_cutoff = [d for d in detailed if not d.get("after_cutoff")]
    exact_after_cutoff = [d for d in after_cutoff if d.get("extracted_upc") == upc]
    exact_before_cutoff = [d for d in before_cutoff if d.get("extracted_upc") == upc]
    mismatched_after_cutoff = [d for d in after_cutoff if d.get("extracted_upc") and d.get("extracted_upc") != upc]

    aeolus = aeolus_rows.get(upc) or {}
    qualifies = ra.qualifies(aeolus) if aeolus else False
    dedup = find_dedup_records(upc)

    reason = None
    if not after_cutoff:
        if before_cutoff:
            reason = "not found in inbox scan from Jun 3 onward; matching email exists only before cutoff"
        else:
            reason = "not found in inbox by UPC search"
    elif not exact_after_cutoff:
        if mismatched_after_cutoff:
            reason = "email found after cutoff, but UPC extraction did not produce this UPC"
        else:
            reason = "email found after cutoff, but UPC could not be extracted"
    elif not aeolus:
        reason = "UPC extracted, but Aeolus batch query returned no row"
    elif not qualifies:
        region = (aeolus.get("user_region") or "").strip().upper() or "N/A"
        source = (aeolus.get("source_type_name") or "").strip() or "N/A"
        tier = (aeolus.get("User Tier") or "").strip() or "N/A"
        if region != "BR":
            reason = f"failed BR filter: Region={region}, Source={source}, Tier={tier}"
        else:
            reason = f"failed quality filter: Region={region}, Source={source}, Tier={tier}"
    elif dedup:
        reason = "qualified but skipped by deduplication / already in posted_claims"
    else:
        reason = "qualified and not deduped — would need deeper run-specific investigation"

    results.append({
        "upc": upc,
        "reason": reason,
        "inbox": {
            "matches_total": len(detailed),
            "matches_after_cutoff": len(after_cutoff),
            "matches_before_cutoff": len(before_cutoff),
            "exact_after_cutoff": len(exact_after_cutoff),
            "exact_before_cutoff": len(exact_before_cutoff),
            "sample_after_cutoff": after_cutoff[:3],
            "sample_before_cutoff": before_cutoff[:3],
        },
        "aeolus": aeolus if aeolus else None,
        "aeolus_qualifies": qualifies,
        "dedup_hits": dedup,
    })

print(json.dumps({
    "cutoff": CUTOFF.isoformat(),
    "target_count": len(TARGETS),
    "results": results,
}, ensure_ascii=False, indent=2))
