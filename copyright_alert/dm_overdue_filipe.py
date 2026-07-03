#!/usr/bin/env python3
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from copyright_alert.tag_managers import (
    read_sheet_values,
    collect_pending,
    today_brt,
    _add_workdays,
    _net_workdays,
    _format_sla_suffix,
    SLA_WORKDAYS,
    ADMIN_UPC_URL_TEMPLATE,
    TRACKER_SHEET_URL
)
from copyright_alert.run_alert import _get_bot_access_token
import urllib.request

def send_dm_post(email, title, content_lines):
    token = _get_bot_access_token()
    if not token:
        print("✗ Could not obtain bot token.")
        return False
    
    content = []
    for line in content_lines:
        if line == "__HR__":
            content.append([{"tag": "hr"}])
        elif isinstance(line, tuple) and line[0] == "link":
            content.append([{"tag": "a", "text": line[1], "href": line[2]}])
        else:
            content.append([{"tag": "text", "text": str(line)}])
            
    payload = {"zh_cn": {"title": title, "content": content}}
    url = "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=email"
    body = json.dumps({
        "receive_id": email,
        "msg_type": "post",
        "content": json.dumps(payload, ensure_ascii=False),
    }).encode("utf-8")
    
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode()
            parsed = json.loads(text)
            print(f"DM result: code={parsed.get('code')} msg={parsed.get('msg')}")
            return parsed.get("code") == 0
    except Exception as e:
        print(f"DM failed: {e}")
        return False

def main():
    values = read_sheet_values("A:Z")
    managers, pending_rows = collect_pending(values)
    
    today = today_brt()
    overdue_cases = []
    
    from copyright_alert.tag_managers import _parse_date_received
    
    for row in pending_rows:
        received = _parse_date_received(row['received'])
        if not received:
            continue
        deadline = _add_workdays(received, SLA_WORKDAYS)
        remaining = _net_workdays(today, deadline)
        
        if remaining < 0:
            overdue_cases.append(row)
            
    print(f"Found {len(overdue_cases)} overdue cases.")
    
    if not overdue_cases:
        print("No overdue cases to DM.")
        return

    lines = [f"The following {len(overdue_cases)} cases have been open for 5+ workdays with no action:", "__HR__"]
    for row in overdue_cases:
        url = ADMIN_UPC_URL_TEMPLATE.format(upc=row['upc'])
        sla = _format_sla_suffix(row['received'], today)
        lines.append(f"• UPC {row['upc']} — {row['title']} ({sla})")
        lines.append(f"  Row: {row['row']} | Link: {url}")
        
    lines.append("__HR__")
    lines.append(("link", "Open the tracker sheet", TRACKER_SHEET_URL))
    
    send_dm_post("filipe.cairo@bytedance.com", "🔴 Overdue Infringement Claims Digest", lines)

if __name__ == "__main__":
    main()
