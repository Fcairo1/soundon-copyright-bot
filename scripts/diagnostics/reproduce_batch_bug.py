#!/usr/bin/env python3
import json, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
from copyright_alert import run_alert as ra

candidates = json.loads(Path('runtime/reconstruct_generic_scan_output.json').read_text(encoding='utf-8'))
unique_upcs = []
seen = set()
for row in candidates:
    upc = str(row.get('upc') or '').strip()
    if not upc or upc == 'N/A' or upc in seen:
        continue
    seen.add(upc)
    unique_upcs.append(upc)
res = ra.batch_query_aeolus_by_upc(unique_upcs, chunk_size=80)
print(json.dumps({
    'unique_upc_count': len(unique_upcs),
    'returned_count': len(res),
    'returned_upcs': list(res.keys())
}, ensure_ascii=False, indent=2))
