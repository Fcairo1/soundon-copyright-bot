#!/usr/bin/env python3
"""Resend the private DM action card for UPC 047752352704 to filipe.cairo.

This intentionally uses the BR workflow card builder/sender and a freshly
constructed case based on the tracker/post history details for the source email
message ID requested by the operator.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert import dm_action_card  # noqa: E402

case = {
    "upc": "047752352704",
    "isrc": "QT8BT2692502",
    "title": "Água Limpa",
    "artist": "Sena, DJ Marques, Kyan",
    "claimant_name": "Warner Music Group (IFPI)",
    "claimant_email": "notices@ifpi.org",
    "source_email_message_id": "eml5OWxHbmpqZ1BzNFpaT1B0QWhIOVUxenYwPQ==",
    "lark_card_message_id": "om_x100b6cda1146aca8e2ce4c91003f0aa",
    "detected_at": "2026-06-16T14:43:13Z",
    "tracker_row": None,
    "ref_id": "ref:_00D0992XChO._500QveWu03:ref",
    "region": "BR",
    "ops_dm_email": "filipe.cairo@bytedance.com",
}

out_path = ROOT / "copyright_alert" / "last_dm_card_047752352704.json"
out_path.write_text(
    json.dumps(dm_action_card.build_dm_action_card(case), ensure_ascii=False, indent=2),
    encoding="utf-8",
)

result = dm_action_card.send_dm_action_card(case, return_result=True)
print(json.dumps(result, ensure_ascii=False, indent=2))

if not result.get("ok"):
    raise SystemExit(1)
