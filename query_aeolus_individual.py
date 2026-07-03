#!/usr/bin/env python3
import json
from pathlib import Path
import os
ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
from copyright_alert import run_alert as ra

UPCS = [
    "5063964388275", "5063964668681", "073011892957", "790566637399", "010735103334",
    "672079828238", "047752485006", "038557586685", "638022968109", "5063964187809",
    "5063964638790", "5063964795035", "049362112921", "052599254098", "601294340430",
    "074417691533", "682106738423", "796728442269", "5063964471855", "5063936513445",
]
rows = []
for upc in UPCS:
    row = ra.query_aeolus(upc, "upc")
    rows.append({
        "upc": upc,
        "aeolus": row or None,
        "qualifies": ra.qualifies(row) if row else False,
    })
print(json.dumps(rows, ensure_ascii=False, indent=2))
