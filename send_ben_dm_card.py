#!/usr/bin/env python3
import json
from copyright_alert import dm_action_card
from copyright_alert.region_guard import assert_region_allowed

case = {
    "message_id": "om_x100b6c8886e600ace10a57d316cadc8",
    "lark_card_message_id": "om_x100b6c8886e600ace10a57d316cadc8",
    "source_email_message_id": "RFIwRndjZy9PQTQ1UTUrRHc3anRlMUJySHM4PQ==",
    "subject": "Possibly Infringing - Notification Warning No 1 - FreesTyle Sounds - Claim 24300814 - ref:_00D0992XChO._500Qves2XJ:ref",
    "upc": "046081844102",
    "isrc": "BKY2C2600124",
    "title": "Lombradão",
    "artist": "DJ GUILHERME BORGES, DJ DERBHEN, DJPIZZABEATS, MC W1",
    "ref_id": "ref:_00D0992XChO._500Qves2XJ:ref",
    "claimant_name": "VICTOR VICTOR BORBA",
    "claimant_email": "victor@ninemusic.com.br",
    "detected_at": "2026-06-24T10:38:00Z",
    "region": "US",
    "ops_dm_email": "bengordonpound.cphn@bytedance.com",
    "ops_dm_open_id": "ou_9cd2b961d55ed59e1b0e79e0b52a677c",
    "ops_dm_chat_id": "oc_842a762dacdea52dd8cd4017da3a94d5",
}
assert_region_allowed(case["ops_dm_chat_id"], {"user_region": case["region"], "upc": case["upc"]}, context="DM action card")
print(json.dumps(dm_action_card.send_dm_action_card(case, return_result=True), ensure_ascii=False))
