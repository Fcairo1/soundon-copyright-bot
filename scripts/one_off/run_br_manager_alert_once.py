#!/usr/bin/env python3
"""One-off BR manager alert runner using tracker AM/BD columns.

Avoids the slow Aeolus fallback path when Aeolus is unavailable, while still
respecting the persistent manager exclusion list through tag_managers helpers.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import copyright_alert.tag_managers as tm
from copyright_alert.manager_exclusions import filter_manager_pairs
from copyright_alert.run_alert import TRACKER_SHEET_URL
import copyright_alert.dm_overdue_filipe as dm


def main():
    values = tm.read_sheet_values("A:Z")
    headers = [tm._norm(h) for h in values[0]] if values else []
    idx = {h: i for i, h in enumerate(headers) if h}

    managers = {}
    pending_rows = []
    no_manager_rows = []

    for r, row in enumerate(values[1:], start=2):
        if not any(tm._norm(c) for c in row):
            continue
        status = tm._cell(row, idx.get("Status"))
        admin = tm._cell(row, idx.get(tm.ADMIN_ACTION_HEADER))
        if not tm._is_pending_row(status, admin):
            continue

        upc = tm._cell(row, idx.get("UPC")) or "N/A"
        title = tm._cell(row, idx.get("Title")) or "(untitled)"
        received = tm._cell(row, idx.get("Date Received"))
        label_uid = tm._cell(row, idx.get("Label UID"))
        pending_rows.append({"row": r, "upc": upc, "title": title, "received": received, "status": status or "(empty)"})

        people = []
        seen = set()
        for col in ("BD", "Label Manager"):
            raw = tm._cell(row, idx.get(col))
            if not raw or raw.upper() == "N/A":
                continue
            for name in [p.strip() for p in raw.split(",") if p.strip()]:
                uname = tm._username_from_display(name)
                key = uname.lower()
                if not uname or key in seen or key in tm.HARD_SKIP_USERNAMES:
                    continue
                seen.add(key)
                people.append((uname, name))
        people = filter_manager_pairs(label_uid, people)

        if not people:
            no_manager_rows.append({"row": r, "upc": upc, "title": title, "received": received})
        for uname, display in people:
            entry = managers.setdefault(uname, {"display": display, "items": []})
            triple = (upc, title, received)
            if triple not in entry["items"]:
                entry["items"].append(triple)

    print(f"Fallback tracker-only manager alert: pending_rows={len(pending_rows)} managers={len(managers)} no_manager_rows={len(no_manager_rows)}", flush=True)
    for uname, info in managers.items():
        upcs = ", ".join(item[0] for item in info["items"])
        print(f"  @{info['display']} ({uname}@{tm.MENTION_DOMAIN}): UPCs {upcs}", flush=True)
    for item in no_manager_rows:
        print(f"  ⚠ No manager assigned: row {item['row']} UPC {item['upc']} — {item['title']}", flush=True)

    group_ok = False
    group_msg_id = ""
    if managers or no_manager_rows:
        card = tm.build_tag_card(managers, no_manager_rows)
        group_ok, group_msg_id = tm.post_card_to_chat(card)
        print(f"GROUP_POST ok={group_ok} message_id={group_msg_id}", flush=True)
    else:
        print("No pending rows to post to group.", flush=True)

    today = tm.today_brt()
    overdue = []
    for row in pending_rows:
        rec = tm._parse_date_received(row["received"])
        if not rec:
            continue
        remaining = tm._net_workdays(today, tm._add_workdays(rec, tm.SLA_WORKDAYS))
        if remaining <= 0:
            overdue.append(row)
    print(f"Overdue cases open 5+ workdays no action: {len(overdue)}", flush=True)

    dm_ok = False
    if overdue:
        lines = [f"The following {len(overdue)} cases are open 5+ workdays with no Admin Action Taken:", "__HR__"]
        for x in overdue:
            lines.append(f"• UPC {x['upc']} — {x['title']} — {tm._format_sla_suffix(x['received'], today)} (row {x['row']}, received {x['received']})")
            lines.append(f"  {tm.ADMIN_UPC_URL_TEMPLATE.format(upc=x['upc'])}")
        lines += ["__HR__", ("link", "Open the tracker sheet", TRACKER_SHEET_URL)]
        dm_ok = dm.send_dm_post("filipe.cairo@bytedance.com", "🔴 BR copyright claims open 5+ workdays", lines)
        print(f"DM_POST ok={dm_ok}", flush=True)
    else:
        print("No overdue DM needed.", flush=True)

    return 0 if (group_ok or not (managers or no_manager_rows)) and (dm_ok or not overdue) else 2


if __name__ == "__main__":
    raise SystemExit(main())
