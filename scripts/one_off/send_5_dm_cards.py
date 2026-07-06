#!/usr/bin/env python3
"""One-off: send Spotify reply DM action cards (Agree/Investigating/Dispute)
privately to filipe.cairo for 5 specific UPCs, using metadata from
posted_claims*.json plus a fresh inbox lookup for claimant_email/date."""

from __future__ import annotations
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert import run_alert as ra
from copyright_alert import dm_action_card

# UPC → tracker row (manually mapped from sheet read).
TARGETS = [
    ("601294340430",  5),
    ("5063964388275", 14),
    ("047752485006",  18),
    ("049362112921",  24),
    ("5063964745795", 33),
]


def load_posted_claims():
    merged = {}
    for fname in ("posted_claims.json", "posted_claims_ap_direitos_br.json"):
        p = ROOT / "copyright_alert" / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            print(f"  ! could not parse {fname}: {e}")
            continue
        for k, v in data.items():
            upc = v.get("upc")
            if upc:
                merged.setdefault(upc, []).append(v)
    return merged


def find_record(claims_by_upc, upc):
    """Pick the most informative record for this UPC."""
    cands = claims_by_upc.get(upc) or []
    # Prefer ones with claimant_email present.
    cands.sort(key=lambda v: (
        0 if v.get("claimant_email") and v.get("claimant_email") not in ("", "N/A") else 1,
        0 if v.get("source_email_message_id") or v.get("source_message_id") else 1,
    ))
    return cands[0] if cands else None


def enrich_with_inbox(rec):
    """If claimant_email is missing, fetch source email and extract fields."""
    src = rec.get("source_email_message_id") or rec.get("source_message_id")
    if not src:
        return rec
    if rec.get("claimant_email") and rec["claimant_email"] not in ("", "N/A"):
        return rec
    try:
        body, meta = ra.fetch_email(src)
        if body:
            ef = ra.extract_fields(body, rec.get("subject", ""), meta)
            for k in ("claimant_email", "claimant_name", "ref_id", "title", "isrc"):
                v = ef.get(k)
                if v and v != "N/A" and not rec.get(k):
                    rec[k] = v
            # also pick a detected_at if absent
            if not rec.get("date"):
                rec["date"] = meta.get("date_formatted") or meta.get("date") or ""
    except Exception as e:
        print(f"  ! enrich_with_inbox failed for {src}: {e!r}")
    return rec


def main():
    claims = load_posted_claims()
    sent = 0
    for upc, row in TARGETS:
        print(f"\n=== UPC {upc} (tracker row {row}) ===")
        rec = find_record(claims, upc)
        if not rec:
            print(f"  ✗ no posted-claim record for UPC {upc}; skipping.")
            continue
        rec = enrich_with_inbox(dict(rec))
        src = rec.get("source_email_message_id") or rec.get("source_message_id") or ""
        case = {
            "upc": upc,
            "isrc": rec.get("isrc", "N/A"),
            "title": rec.get("title", "N/A"),
            "artist": rec.get("artist", "N/A"),
            "claimant_name": rec.get("claimant_name", "N/A"),
            "claimant_email": rec.get("claimant_email", "N/A"),
            "source_email_message_id": src,
            "lark_card_message_id": rec.get("message_id") or rec.get("posted_message_id") or "",
            "detected_at": rec.get("date") or "",
            "tracker_row": row,
            "ref_id": rec.get("ref_id", "N/A"),
        }
        print("  case:", json.dumps({k: case[k] for k in (
            "upc","isrc","title","artist","claimant_name","claimant_email",
            "ref_id","source_email_message_id","detected_at","tracker_row")},
            ensure_ascii=False))
        ok = dm_action_card.send_dm_action_card(case)
        print(f"  → DM_SEND_OK={ok}")
        if ok:
            sent += 1
    print(f"\nDone. {sent}/{len(TARGETS)} cards sent to filipe.cairo.")


if __name__ == "__main__":
    main()
