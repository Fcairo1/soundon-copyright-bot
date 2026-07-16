#!/usr/bin/env python3
"""Persistent UPC exclusion helpers for alert scans and digests."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from copyright_alert.state_io import update_json_state

ROOT = Path(__file__).resolve().parents[1]
# F5: Keep the state file inside the package dir (consistent with
# manager_exclusions.json and the other *.json state files) instead of a third
# state location under <repo>/data/. The legacy path is migrated on first load.
EXCLUSIONS_FILE = ROOT / "copyright_alert" / "upc_exclusions.json"
_LEGACY_EXCLUSIONS_FILE = ROOT / "data" / "upc_exclusions.json"

_UPC_RE = re.compile(r"^\d{12,13}$")


def normalize_upc(value: str) -> str:
    """Return a UPC-like value containing only digits, or empty if invalid."""
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if _UPC_RE.match(digits):
        return digits
    return ""


def _normalize_record(record: dict) -> dict:
    upc = normalize_upc((record or {}).get("upc"))
    if not upc:
        return {}
    return {
        "upc": upc,
        "reason": str((record or {}).get("reason") or "").strip(),
        "added_by": str((record or {}).get("added_by") or "unknown").strip() or "unknown",
        "added_at": str((record or {}).get("added_at") or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _payload_to_data(payload) -> Dict[str, dict]:
    items = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("exclusions"), list):
            items = payload.get("exclusions") or []
        else:
            for upc, record in payload.items():
                if isinstance(record, dict):
                    items.append({**record, "upc": record.get("upc") or upc})
                else:
                    items.append({"upc": upc})

    data: Dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        record = _normalize_record(item)
        if record:
            data[record["upc"]] = record
    return data


def _data_to_records(data: Dict[str, dict]) -> List[dict]:
    records: List[dict] = []
    for upc in sorted((data or {}).keys()):
        record = _normalize_record({**(data.get(upc) or {}), "upc": upc})
        if record:
            records.append(record)
    return records


def _migrate_legacy_exclusions_file() -> None:
    """F5: Move the state file from the old <repo>/data/ location into the
    package dir on first use. Runs at most once (skipped once the new file
    exists). Best-effort: any failure leaves both files untouched.
    """
    try:
        if EXCLUSIONS_FILE.exists() or not _LEGACY_EXCLUSIONS_FILE.exists():
            return
        EXCLUSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LEGACY_EXCLUSIONS_FILE.replace(EXCLUSIONS_FILE)
    except Exception:
        pass


def load_upc_exclusions() -> Dict[str, dict]:
    """Load exclusions keyed by normalized UPC.

    Accepts both the canonical list format and a defensive legacy dict format.
    """
    _migrate_legacy_exclusions_file()
    if not EXCLUSIONS_FILE.exists():
        return {}
    try:
        payload = json.loads(EXCLUSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return _payload_to_data(payload)


def save_upc_exclusions(data: Dict[str, dict]) -> None:
    records = _data_to_records(data)

    def replace(_current):
        return records

    update_json_state(EXCLUSIONS_FILE, replace, default=list, ensure_ascii=False, indent=2)


def add_upc_exclusion(upc: str, reason: str = "", added_by: str = "unknown") -> Tuple[bool, dict]:
    norm = normalize_upc(upc)
    if not norm:
        return False, {}
    record = {
        "upc": norm,
        "reason": str(reason or "").strip(),
        "added_by": str(added_by or "unknown").strip() or "unknown",
        "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def mutate(payload):
        data = _payload_to_data(payload)
        data[norm] = record
        return _data_to_records(data)

    _migrate_legacy_exclusions_file()
    update_json_state(EXCLUSIONS_FILE, mutate, default=list, ensure_ascii=False, indent=2)
    return True, record


def remove_upc_exclusion(upc: str) -> Tuple[bool, dict]:
    norm = normalize_upc(upc)
    if not norm:
        return False, {}
    removed = {}

    def mutate(payload):
        nonlocal removed
        data = _payload_to_data(payload)
        removed = data.pop(norm, {})
        return _data_to_records(data)

    _migrate_legacy_exclusions_file()
    update_json_state(EXCLUSIONS_FILE, mutate, default=list, ensure_ascii=False, indent=2)
    return True, removed


def is_upc_excluded(upc: str) -> bool:
    norm = normalize_upc(upc)
    return bool(norm and norm in load_upc_exclusions())


def describe_upc_exclusions() -> List[dict]:
    # F3: Load the exclusion store once instead of re-reading the JSON file
    # N+1 times (once for the keys, once per record).
    data = load_upc_exclusions()
    return [data[upc] for upc in sorted(data.keys())]
