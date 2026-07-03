#!/usr/bin/env python3
"""Retry the two BR cards that failed on 2026-06-26 due to chat membership/config mismatch."""
import csv
import io
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)

from copyright_alert import run_alert as ra  # noqa: E402

# Diagnosis showed the current BR production group named "AP Direitos BR" is this chat.
TARGET_CHAT_ID = "oc_fd2e43d6451f8d87adb4cd4ceefa7816"
POSTED_FILE = "runtime/posted_claims_ap_direitos_br.json"
TARGETS = [
    {"upc": "5063963044431", "message_id": "YXJ4VDZMNUlhSS9tamNSR0xxUGsxZVZvcFowPQ==", "date": "2026-06-25T20:58:21Z"},
    {"upc": "5063965032139", "message_id": "Nzlnd09OZEh3SXV0NXNoU1QzYTBIRlVIaXFjPQ==", "date": "2026-06-26T07:23:41Z"},
]

ra.TARGET_CHAT_ID = TARGET_CHAT_ID
ra.POSTED_CLAIMS_FILE = POSTED_FILE
ra.EXCLUDED_MENTIONS = {"esteban.mora", "filipe.cairo"}
ra.CURRENT_REGION = "BR"
ra.QUALIFY_COUNTRIES = {"BR"}


def strip_row_prefix(line: str):
    import re
    return re.sub(r"^\[row=\d+\]\s*", "", line)


def fetch_tracker_keys():
    cmd = [
        "lark-cli", "sheets", "+csv-get",
        "--url", ra.TRACKER_SHEET_URL,
        "--sheet-id", ra.TRACKER_SHEET_ID,
        "--range", "A1:Q500",
    ]
    import subprocess
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        print("tracker-key read failed", (res.stdout + res.stderr)[:500], flush=True)
        return set()
    parsed = ra.parse_lark_json(res.stdout) or {}
    annotated = ((parsed.get("data") or {}).get("annotated_csv") or "")
    lines = [strip_row_prefix(line) for line in annotated.splitlines() if line.strip()]
    if not lines:
        return set()
    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows = list(reader)
    keys = set()
    for row in rows[1:]:
        def cell(i): return str(row[i] if len(row) > i else "").strip()
        keys.add((cell(0), cell(1), cell(2), cell(8), cell(13)))
    return keys


def tracker_key(ef, ar):
    def norm(v): return str(v if v is not None else "").strip()
    return (
        norm(ef.get("upc", "N/A")),
        norm(ef.get("isrc", "N/A")),
        norm(ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A")),
        norm(ef.get("claimant_name", "N/A")),
        norm(ef.get("date_received", "N/A")),
    )


def main():
    aeolus_by_upc = ra.batch_query_aeolus_by_upc([t["upc"] for t in TARGETS])
    tracker_keys = fetch_tracker_keys()
    results = []
    for target in TARGETS:
        upc = target["upc"]
        body, meta = ra.fetch_email(target["message_id"])
        ef = ra.extract_fields(body, meta.get("subject", ""), meta)
        ar = aeolus_by_upc.get(upc) or {}
        if not ar:
            results.append({"upc": upc, "status": "skipped_no_aeolus"})
            continue
        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"
        if not ra.qualifies(ar):
            results.append({"upc": upc, "status": "skipped_not_qualifying", "region": ar.get("user_region"), "source": ar.get("source_type_name"), "tier": ar.get("User Tier")})
            continue
        claim_key = ra.claim_key(ef, ar, meta.get("subject", ""))
        if ra.is_claim_already_posted(claim_key):
            results.append({"upc": upc, "status": "already_posted", "claim_key": claim_key})
            continue
        card = ra.build_card(ef, ar)
        ok, message_id = ra.post_card(card, ar, upc=upc, context="BR retry group post")
        if not ok or not message_id:
            results.append({"upc": upc, "status": "post_failed"})
            continue
        card = ra.build_card(ef, ar, lark_message_id=message_id)
        patch_ok = ra.patch_card_message(message_id, card)
        tracker_logged = False
        tk = tracker_key(ef, ar)
        if tk not in tracker_keys:
            tracker_logged = ra.append_tracker_row(ef, ar, message_id, status="")
            if tracker_logged:
                tracker_keys.add(tk)
        record = {
            "message_id": message_id,
            "source_email_message_id": target["message_id"],
            "subject": meta.get("subject", ""),
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "ref_id": ef.get("ref_id", "N/A"),
            "date": target.get("date") or meta.get("date"),
            "region": (ar.get("user_region") or "").strip().upper(),
            "source": ar.get("source_type_name", "N/A"),
            "tier": ar.get("User Tier", "N/A"),
            "chat_id": TARGET_CHAT_ID,
        }
        ra._save_posted_claim(claim_key, record)
        results.append({"upc": upc, "status": "posted", "message_id": message_id, "patch_ok": patch_ok, "tracker_logged": tracker_logged, "claim_key": claim_key, "title": record["title"], "artist": record["artist"], "source": record["source"], "tier": record["tier"]})
    print("__RESULT__")
    print(json.dumps({"target_chat_id": TARGET_CHAT_ID, "posted_file": POSTED_FILE, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
