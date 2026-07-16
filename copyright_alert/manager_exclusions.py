#!/usr/bin/env python3
"""Persistent manager exclusion helpers for copyright alert routing."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from copyright_alert.state_io import update_json_state

ROOT = Path(__file__).resolve().parents[1]
EXCLUSIONS_FILE = ROOT / "copyright_alert" / "manager_exclusions.json"

# Special key in manager_exclusions.json that applies to *every* label UID.
# Managers listed here are permanently skipped from all alert tagging logic
# regardless of which release / label they own.
GLOBAL_UID = "__global__"

# Hardcoded global blocklist (always applied, even if manager_exclusions.json
# is reset). Keep entries lowercase, in `first.last` username form.
HARDCODED_GLOBAL_EXCLUSIONS = ("diego.meleiro", "carla.figlia", "eduardo.praca")


def _norm_uid(label_uid: str) -> str:
    return str(label_uid or "").strip()


def _clean_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.lstrip("@").strip()
    if text.lower().endswith("@bytedance.com"):
        text = text[:-len("@bytedance.com")]
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _identifier_aliases(value: str) -> set[str]:
    cleaned = _clean_identifier(value)
    if not cleaned:
        return set()
    lowered = cleaned.lower()
    collapsed_space = re.sub(r"\s+", " ", lowered).strip()
    aliases = {lowered, collapsed_space}
    if " " in collapsed_space:
        aliases.add(collapsed_space.replace(" ", "."))
        aliases.add(collapsed_space.replace(" ", "_"))
    if "." in lowered:
        aliases.add(lowered.replace(".", " "))
    if "_" in lowered:
        aliases.add(lowered.replace("_", " "))
    return {alias.strip() for alias in aliases if alias.strip()}


def load_exclusions() -> dict:
    try:
        if EXCLUSIONS_FILE.exists():
            data = json.loads(EXCLUSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                normalized = {}
                for uid, managers in data.items():
                    uid_key = _norm_uid(uid)
                    if not uid_key:
                        continue
                    values = []
                    for manager in managers or []:
                        cleaned = _clean_identifier(manager)
                        if cleaned and cleaned not in values:
                            values.append(cleaned)
                    normalized[uid_key] = values
                return normalized
    except Exception:
        pass
    return {}


def save_exclusions(payload: dict) -> None:
    def replace(_current):
        return payload if isinstance(payload, dict) else {}

    update_json_state(EXCLUSIONS_FILE, replace, default=dict, ensure_ascii=False, indent=2)


def ensure_file() -> None:
    if not EXCLUSIONS_FILE.exists():
        save_exclusions({})


def add_exclusion(label_uid: str, managers: Sequence[str]) -> Tuple[bool, List[str]]:
    uid = _norm_uid(label_uid)
    if not uid:
        return False, []
    added = []

    def mutate(data):
        nonlocal added
        if not isinstance(data, dict):
            data = {}
        current = data.setdefault(uid, [])
        for manager in managers:
            cleaned = _clean_identifier(manager)
            if cleaned and cleaned not in current:
                current.append(cleaned)
                added.append(cleaned)
        current.sort(key=lambda item: item.lower())
        return data

    update_json_state(EXCLUSIONS_FILE, mutate, default=dict, ensure_ascii=False, indent=2)
    return True, added


def remove_exclusion(label_uid: str, managers: Sequence[str]) -> Tuple[bool, List[str]]:
    uid = _norm_uid(label_uid)
    if not uid:
        return False, []
    target_aliases = set()
    for manager in managers:
        target_aliases.update(_identifier_aliases(manager))
    removed = []

    def mutate(data):
        nonlocal removed
        if not isinstance(data, dict):
            data = {}
        current = data.get(uid, [])
        if not current:
            return data
        kept = []
        for item in current:
            aliases = _identifier_aliases(item)
            if aliases & target_aliases:
                removed.append(item)
            else:
                kept.append(item)
        if kept:
            data[uid] = kept
        else:
            data.pop(uid, None)
        return data

    update_json_state(EXCLUSIONS_FILE, mutate, default=dict, ensure_ascii=False, indent=2)
    return True, removed


def _global_exclusion_aliases(data: dict | None = None) -> set[str]:
    """All aliases for managers that are excluded from every label UID."""
    payload = data if data is not None else load_exclusions()
    aliases: set[str] = set()
    for item in HARDCODED_GLOBAL_EXCLUSIONS:
        aliases.update(_identifier_aliases(item))
    for item in payload.get(GLOBAL_UID) or []:
        aliases.update(_identifier_aliases(item))
    return aliases


def is_manager_excluded(label_uid: str, *manager_candidates: str) -> bool:
    candidate_aliases: set[str] = set()
    for candidate in manager_candidates:
        candidate_aliases.update(_identifier_aliases(candidate))
    if not candidate_aliases:
        return False
    data = load_exclusions()
    # Global block list applies regardless of label_uid.
    if _global_exclusion_aliases(data) & candidate_aliases:
        return True
    uid = _norm_uid(label_uid)
    if not uid:
        return False
    excluded = data.get(uid) or []
    for item in excluded:
        if _identifier_aliases(item) & candidate_aliases:
            return True
    return False


def filter_manager_pairs(label_uid: str, manager_pairs: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    filtered = []
    for username, display_name in manager_pairs:
        if is_manager_excluded(label_uid, username, display_name, f"{username}@bytedance.com"):
            continue
        filtered.append((username, display_name))
    return filtered


def total_exclusion_count(data: dict | None = None) -> int:
    payload = data if data is not None else load_exclusions()
    return sum(len(managers or []) for managers in payload.values())


def manager_uids(manager: str, data: dict | None = None) -> List[str]:
    payload = data if data is not None else load_exclusions()
    target_aliases = _identifier_aliases(manager)
    matches = []
    for uid, managers in sorted(payload.items(), key=lambda item: item[0]):
        if any(_identifier_aliases(item) & target_aliases for item in managers or []):
            matches.append(uid)
    return matches
