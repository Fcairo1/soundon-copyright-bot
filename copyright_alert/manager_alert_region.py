#!/usr/bin/env python3
"""Region-aware manager alert runner for copyright infringement trackers.

This module reuses the BR manager alert implementation in tag_managers.py and
adds region-specific runtime configuration plus Ops digest DM delivery.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from copyright_alert import bot_runtime  # noqa: E402
from copyright_alert import tag_managers as tm  # noqa: E402


REGION_ALERT_CONFIGS: Dict[str, Dict[str, Any]] = {
    "SPLA": {
        "local_tz": "America/Mexico_City",
        "local_label": "2PM Mexico City time",
        "schedule_note": "Wed/Fri 14:00 America/Mexico_City = Asia/Shanghai cron 0 0 21 * * 3,5",
        "digest_title": "SPLA Manager Alert Digest",
        "digest_receive_id_type": "chat_id",
        "digest_receive_id": "oc_48de5eacf06bffee6cd4aa422e1e9855",  # Bernardo Sanchez confirmed DM chat
        "display_tracker_url_key": "tracker_wiki_url",
    },
    "US": {
        "local_tz": "America/Los_Angeles",
        "local_label": "2PM Los Angeles time",
        "schedule_note": "Wed/Fri 14:00 America/Los_Angeles = Asia/Shanghai cron 0 0 5 * * 4,6",
        "digest_title": "US Manager Alert Digest",
        "digest_receive_id_type": "chat_id",
        "digest_receive_id": "oc_842a762dacdea52dd8cd4017da3a94d5",  # Ben Gordon-Pound confirmed DM chat
        "display_tracker_url_key": "tracker_url",
        "post_group_alert": False,
    },
}


def _local_today(tz_name: str):
    return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()


def _patch_tag_manager_runtime(region: str, cfg: Dict[str, Any], alert_cfg: Dict[str, Any], *, for_display: bool = False) -> None:
    """Patch tag_managers' imported constants after bot_runtime region setup.

    tag_managers imports run_alert constants by value, so configuring run_alert is
    not enough. This keeps the proven BR manager-alert logic while pointing it at
    the requested regional tracker and chat.
    """
    tm.TARGET_CHAT_ID = cfg["chat_id"]
    tm.TRACKER_SHEET_ID = cfg["sheet_id"]
    if for_display:
        display_key = alert_cfg.get("display_tracker_url_key") or "tracker_url"
        tm.TRACKER_SHEET_URL = cfg.get(display_key) or cfg["tracker_url"]
    else:
        tm.TRACKER_SHEET_URL = cfg["tracker_url"]


def _build_digest_card(region: str, cfg: Dict[str, Any], alert_cfg: Dict[str, Any], managers: dict, pending_rows: list) -> dict:
    tracker_url = cfg.get(alert_cfg.get("display_tracker_url_key") or "tracker_url") or cfg["tracker_url"]
    manager_count = len(managers)
    pending_count = len(pending_rows)
    item_count = sum(len(info.get("items") or []) for info in managers.values())

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**{region} manager alert completed.**\n"
                    f"Pending rows: **{pending_count}**\n"
                    f"Managers tagged: **{manager_count}**\n"
                    f"Manager-case assignments: **{item_count}**"
                ),
            },
        },
        {"tag": "hr"},
    ]

    if managers:
        summary_lines = []
        for uname, info in sorted(managers.items(), key=lambda item: item[1].get("display", item[0]).lower()):
            upcs = ", ".join(item[0] for item in info.get("items") or []) or "N/A"
            summary_lines.append(f"• {info.get('display') or uname} ({uname}) — {upcs}")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(summary_lines)}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "No managers needed tagging in this run."}})

    elements.extend([
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "lark_md", "content": f"[Open the {region} tracker]({tracker_url})"}]},
    ])

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": alert_cfg["digest_title"]}},
        "elements": elements,
    }


def run_region_manager_alert(region: str) -> dict:
    region = region.upper()
    if region not in REGION_ALERT_CONFIGS:
        raise ValueError(f"Unsupported manager alert region: {region}")

    dry_run = "--dry-run" in sys.argv
    skip_dm = "--skip-dm" in sys.argv
    alert_cfg = REGION_ALERT_CONFIGS[region]
    cfg = bot_runtime.configure_region(region)
    _patch_tag_manager_runtime(region, cfg, alert_cfg, for_display=False)

    print(
        f"manager_alert_{region.lower()} — started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"(dry_run={dry_run}, skip_dm={skip_dm})",
        flush=True,
    )
    print(f"Region: {region}", flush=True)
    print(f"Tracker read URL: {cfg['tracker_url']}", flush=True)
    if cfg.get("tracker_wiki_url"):
        print(f"Tracker wiki URL: {cfg['tracker_wiki_url']}", flush=True)
    post_group_alert = alert_cfg.get("post_group_alert", True)
    if post_group_alert:
        print(f"Alert group: {cfg['chat_id']}", flush=True)
    else:
        print("Alert group: disabled for this region", flush=True)
    print(f"Intended schedule: {alert_cfg['schedule_note']}", flush=True)

    values = tm.read_sheet_values("A:Z")
    managers, pending_rows, no_manager_rows = tm.collect_pending(values)

    print(f"\nPending rows: {len(pending_rows)} | Managers to tag: {len(managers)}", flush=True)
    for uname, info in managers.items():
        upcs = ", ".join(item[0] for item in info["items"])
        print(f"  @{info['display']} ({uname}@{tm.MENTION_DOMAIN}): UPCs {upcs}", flush=True)

    print(f"No-manager pending rows: {len(no_manager_rows)}", flush=True)
    for item in no_manager_rows:
        print(f"  ⚠ No manager assigned: UPC {item['upc']} — {item['title']}", flush=True)

    # Use the region-local date for SLA suffixes in cards.
    tm.today_brt = lambda: _local_today(alert_cfg["local_tz"])
    _patch_tag_manager_runtime(region, cfg, alert_cfg, for_display=True)

    if not managers and not no_manager_rows:
        print("\n✓ Nothing pending to tag. No group message posted.", flush=True)
        digest_card = _build_digest_card(region, cfg, alert_cfg, managers, pending_rows)
        if not dry_run and not skip_dm:
            ok, msg_id = tm._post_card(digest_card, alert_cfg["digest_receive_id"], alert_cfg["digest_receive_id_type"])
            print(f"Digest DM {'sent' if ok else 'failed'}: message_id={msg_id}", flush=True)
        elif dry_run:
            print("\n[DRY-RUN] Digest card that WOULD be DM'd:", flush=True)
            print(json.dumps(digest_card, ensure_ascii=False, indent=2), flush=True)
        return {"region": region, "pending_rows": len(pending_rows), "managers": 0, "no_manager_rows": 0, "posted": False}

    # Pass the region explicitly so US cards never depend on the mutable
    # run_alert.CURRENT_REGION global for the no-@mention behavior.
    tag_card = tm.build_tag_card(managers, no_manager_rows, region=region)
    digest_card = _build_digest_card(region, cfg, alert_cfg, managers, pending_rows)

    if dry_run:
        if post_group_alert:
            print("\n[DRY-RUN] Tag card that WOULD be posted to group:", flush=True)
            print(json.dumps(tag_card, ensure_ascii=False, indent=2), flush=True)
        else:
            print("\n[DRY-RUN] Group tagging message is disabled for this region.", flush=True)
        if not skip_dm:
            print("\n[DRY-RUN] Digest card that WOULD be DM'd:", flush=True)
            print(json.dumps(digest_card, ensure_ascii=False, indent=2), flush=True)
        return {"region": region, "pending_rows": len(pending_rows), "managers": len(managers), "posted": False, "dry_run": True}

    group_ok = False
    group_msg_id = ""
    if post_group_alert:
        group_ok, group_msg_id = tm._post_card(tag_card, cfg["chat_id"], "chat_id")
        print(f"\n{'✅ Posted' if group_ok else '✗ Failed to post'} group tagging message. message_id={group_msg_id}", flush=True)
    else:
        print("\nGroup tagging message skipped for this region.", flush=True)

    dm_ok = False
    dm_msg_id = ""
    if not skip_dm:
        dm_ok, dm_msg_id = tm._post_card(digest_card, alert_cfg["digest_receive_id"], alert_cfg["digest_receive_id_type"])
        print(f"{'✅ Sent' if dm_ok else '✗ Failed to send'} Ops digest DM. message_id={dm_msg_id}", flush=True)

    return {
        "region": region,
        "pending_rows": len(pending_rows),
        "managers": len(managers),
        "posted": group_ok,
        "message_id": group_msg_id,
        "digest_sent": dm_ok,
        "digest_message_id": dm_msg_id,
    }
