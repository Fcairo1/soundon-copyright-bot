#!/usr/bin/env python3
"""Resend the Spotify-reply DM action card for UPC 5063963471855 to filipe.cairo
using the latest known values (source_email_message_id + tracker_row) from the
most recent click in the callback daemon log."""
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
    "source_email_message_id": "OHU1QmhhcE5OVXJvQm1rOUp6dkhWYy9CWFUwPQ==",
    "lark_card_message_id": "om_x100b6c673f4448a8e2d358b2380930a",
    "detected_at": "2026-06-16",
    "tracker_row": 38,
    "ref_id": "",
}

with open(ROOT / "copyright_alert" / "last_dm_card.json", "w") as f:
    json.dump(dm_action_card.build_dm_action_card(case), f, ensure_ascii=False, indent=1)

ok = dm_action_card.send_dm_action_card(case)
print("DM_SEND_OK=" + str(ok))
