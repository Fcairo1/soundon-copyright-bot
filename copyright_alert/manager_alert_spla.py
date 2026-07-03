#!/usr/bin/env python3
"""SPLA Manager Alert — Wed & Fri at 2PM Mexico City time.
Asia/Shanghai cron: 0 0 21 * * 3,5
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copyright_alert import manager_alert_region

if __name__ == "__main__":
    manager_alert_region.run_region_manager_alert("SPLA")
