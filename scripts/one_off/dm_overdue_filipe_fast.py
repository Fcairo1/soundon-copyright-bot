#!/usr/bin/env python3
import json, os, sys, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
from copyright_alert.tag_managers import read_sheet_values, today_brt, _parse_date_received, _add_workdays, _net_workdays, _format_sla_suffix, SLA_WORKDAYS, ADMIN_UPC_URL_TEMPLATE
from copyright_alert.run_alert import _get_bot_access_token, TRACKER_SHEET_URL
ADMIN_ACTION_HEADER='Admin Action Taken'
PENDING_STATUSES={'🔍 Investigating',''}

def norm(v): return str(v if v is not None else '').strip()
def cell(row,i): return norm(row[i]) if i is not None and len(row)>i else ''

def send(email, title, lines):
    token=_get_bot_access_token()
    content=[]
    for line in lines:
        if line=='__HR__': content.append([{'tag':'hr'}])
        elif isinstance(line, tuple): content.append([{'tag':'a','text':line[1],'href':line[2]}])
        else: content.append([{'tag':'text','text':str(line)}])
    payload={'zh_cn':{'title':title,'content':content}}
    body=json.dumps({'receive_id':email,'msg_type':'post','content':json.dumps(payload,ensure_ascii=False)}).encode('utf-8')
    req=urllib.request.Request('https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=email',data=body,method='POST',headers={'Content-Type':'application/json; charset=utf-8','Authorization':f'Bearer {token}'})
    with urllib.request.urlopen(req,timeout=60) as resp:
        data=json.loads(resp.read().decode())
        print(f"DM result: HTTP {resp.status} code={data.get('code')} msg={data.get('msg')} message_id={(data.get('data') or {}).get('message_id')}")
        return data.get('code')==0

values=read_sheet_values('A:Z')
headers=[norm(h) for h in values[0]] if values else []
idx={h:i for i,h in enumerate(headers) if h}
today=today_brt()
overdue=[]
for r,row in enumerate(values[1:],start=2):
    if not any(norm(c) for c in row): continue
    status=cell(row,idx.get('Status'))
    admin=cell(row,idx.get(ADMIN_ACTION_HEADER))
    if status not in PENDING_STATUSES or admin: continue
    received_raw=cell(row,idx.get('Date Received'))
    received=_parse_date_received(received_raw)
    if not received: continue
    deadline=_add_workdays(received,SLA_WORKDAYS)
    remaining=_net_workdays(today,deadline)
    if remaining<=0:
        overdue.append({'row':r,'upc':cell(row,idx.get('UPC')) or 'N/A','title':cell(row,idx.get('Title')) or 'N/A','received':received_raw,'sla':_format_sla_suffix(received_raw,today)})
print(f'Found {len(overdue)} cases open 5+ workdays with no action.')
if overdue:
    lines=[f'The following {len(overdue)} cases are open 5+ workdays with no Admin Action Taken:', '__HR__']
    for x in overdue:
        lines.append(f"• UPC {x['upc']} — {x['title']} — {x['sla']} (row {x['row']}, received {x['received']})")
        lines.append(f"  {ADMIN_UPC_URL_TEMPLATE.format(upc=x['upc'])}")
    lines += ['__HR__', ('link','Open the tracker sheet',TRACKER_SHEET_URL)]
    send('filipe.cairo@bytedance.com','🔴 BR copyright claims open 5+ workdays',lines)
