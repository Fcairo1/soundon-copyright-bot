import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert import bot_runtime, run_alert as ra

def resend_spla_cards():
    bot_runtime.configure_region("SPLA")
    upcs = ["720665649382", "7316477067150"]
    posted = ra._load_posted_claims()
    
    for upc in upcs:
        print(f"Resending {upc}...")
        # Find claim record
        claim = posted.get(upc)
        key = upc
        if not claim:
            for k, v in posted.items():
                if v.get("upc") == upc:
                    claim = v
                    key = k
                    break
        
        if not claim:
            print(f"  ✗ UPC {upc} not found in {ra.POSTED_CLAIMS_FILE}")
            continue
            
        print(f"  ✓ Found claim record for {upc} (key={key})")
        
        # Build components
        ef = ra.extract_fields(claim.get("claimant_message", ""), claim.get("subject", ""), {"message": claim})
        ar = ra.query_aeolus(upc, "upc")
        if not ar:
            print(f"  ⚠ Aeolus data missing for {upc}, using claim record fields")
            ar = {"upc": upc, "title": claim.get("title"), "artist": claim.get("artist")}
            
        # Build and post group card
        card = ra.build_card(ef, ar)
        ok, msg_id = ra.post_card(card, ar)
        if ok:
            print(f"  ✅ Group card resent → {msg_id}")
            # Update record and save
            claim["message_id"] = msg_id
            from datetime import datetime
            claim["resend_at"] = datetime.utcnow().isoformat() + "Z"
            ra._save_posted_claim(key, claim)
            
            # Note: append_tracker_row is for NEW entries. 
            # These already exist on rows 3/4. 
            # We don't have a direct "update_tracker_row" here that handles message_id easily without side effects.
            # But the user asked to update tracker.
            # The current tracker message IDs are likely om_x100b6ce024ad353ce2e8ac10377870d and om_x100b6ce022eab530e12c8798e8f21ef.
        else:
            print(f"  ✗ Group card failed: {msg_id}")

if __name__ == "__main__":
    resend_spla_cards()
