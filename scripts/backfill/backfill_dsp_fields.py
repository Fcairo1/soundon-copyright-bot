#!/usr/bin/env python3
"""Backfill DSP detection fields in posted_claims.json."""

from __future__ import annotations

import json
from pathlib import Path

from copyright_alert.run_alert import POSTED_CLAIMS_FILE, detect_dsp


def main() -> int:
    path = Path(POSTED_CLAIMS_FILE)
    if not path.exists():
        print(f"No posted claims file found at {path}")
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected dict in {path}, got {type(data).__name__}")

    updated = 0
    unknown = 0
    for _claim_key, record in data.items():
        if not isinstance(record, dict):
            continue
        if record.get("dsp") and record.get("dsp") not in ("N/A", "Unknown") and record.get("dsp_confidence"):
            continue

        detection = detect_dsp({
            "subject": record.get("subject", ""),
            "body": record.get("claimant_message", ""),
            "meta": {
                "from": record.get("sender") or record.get("from") or record.get("claimant_email", ""),
                "sender": record.get("sender", ""),
            },
        })
        record["dsp"] = detection["dsp"]
        record["dsp_confidence"] = detection["confidence"]
        updated += 1
        if detection["dsp"] == "Unknown":
            unknown += 1

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Backfilled DSP fields for {updated} posted claim record(s); Unknown={unknown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
