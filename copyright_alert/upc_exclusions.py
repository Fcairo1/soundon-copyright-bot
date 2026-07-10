#!/usr/bin/env python3
"""Persistent UPC exclusion helpers for alert scans and digests."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
EXCLUSIONS_FILE = ROOT / "data" / "upc_exclusions.json"

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


def load_upc_exclusions() -> Dict[str, dict]:
    """Load exclusions keyed by normalized UPC.

    Accepts both the canonical list format and a defensive legacy dict format.
    """
    if not EXCLUSIONS_FILE.exists():
        return {}
    try:
        payload = json.loads(EXCLUSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

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


def save_upc_exclusions(data: Dict[str, dict]) -> None:
    EXCLUSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    records: List[dict] = []
    for upc in sorted((data or {}).keys()):
        record = _normalize_record({**(data.get(upc) or {}), "upc": upc})
        if record:
            records.append(record)
    EXCLUSIONS_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def add_upc_exclusion(upc: str, reason: str = "", added_by: str = "unknown") -> Tuple[bool, dict]:
    norm = normalize_upc(upc)
    if not norm:
        return False, {}
    data = load_upc_exclusions()
    record = {
        "upc": norm,
        "reason": str(reason or "").strip(),
        "added_by": str(added_by or "unknown").strip() or "unknown",
        "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data[norm] = record
    save_upc_exclusions(data)
    return True, record


def remove_upc_exclusion(upc: str) -> Tuple[bool, dict]:
    norm = normalize_upc(upc)
    if not norm:
        return False, {}
    data = load_upc_exclusions()
    removed = data.pop(norm, {})
    save_upc_exclusions(data)
    return True, removed


def is_upc_excluded(upc: str) -> bool:
    norm = normalize_upc(upc)
    return bool(norm and norm in load_upc_exclusions())


def describe_upc_exclusions() -> List[dict]:
    return [load_upc_exclusions()[upc] for upc in sorted(load_upc_exclusions().keys())]
