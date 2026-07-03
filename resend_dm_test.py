#!/usr/bin/env python3
"""Re-send the DM action card for the previously-found test case (UPC 5063963471855,
claimant "M M Fusaro") to verify the form-container rendering fix. NO inbox scan."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from copyright_alert import dm_action_card

case = {
    "upc": "5063963471855",
    "isrc": "QT8BU2643641",
    "title": "Berço do Crime",
    "artist": "PAVUNA, Bebeto, Renanzin, Erribê",
    "claimant_name": "M M Fusaro",
    "claimant_email": "nmfusaro@onerpm.com",
    "source_email_message_id": "NEM1ZGI2ZjE4Zit2MFF2S0dmM3Q4Vy9NeGRZPQ==",
    "lark_card_message_id": "",          # test: no group card linked
    "detected_at": "2026-06-16",
    "tracker_row": None,                  # test: not writing to sheet
}

# Save the rendered card for inspection.
with open(ROOT / "copyright_alert" / "last_dm_card.json", "w") as f:
    json.dump(dm_action_card.build_dm_action_card(case), f, ensure_ascii=False, indent=1)

ok = dm_action_card.send_dm_action_card(case)
print("DM_SEND_OK=" + str(ok))

if __name__ == "__main__":
    pass
