#!/usr/bin/env python3
"""Persistent manager exclusion helpers for copyright alert routing.

Storage backend: a shared "Manager Exclusions" Lark sheet (flat A:B layout,
header row `UID | Manager`, UID="__global__" for a global exclusion) instead
of a local JSON file — the acrcloud-dashboard's Infringement Claims tab reads
and writes the same sheet, so a manager excluded from either place takes
effect in both without needing a network path between the two separate
deployments. Every public function below keeps its original signature so
tag_managers.py, bot_runtime.py's /exclude /unexclude /exceptions commands,
and anything else importing this module keep working unchanged.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Iterable, List, Sequence, Tuple

from copyright_alert.lark_auth import (
    extract_sheet_values,
    get_user_access_token,
    request_json_with_auth_retry,
    sheet_values_api,
)

# Shared "Manager Exclusions" sheet — also shared with the acrcloud-dashboard's
# Lark app ("ACRCloud Dashboard") for edit access; see acrcloud-dashboard's
# MANAGER_EXCLUSIONS_SHEET_TOKEN env var for the dashboard side of this. The
# tab (sheet_id) is auto-discovered from the first tab.
EXCLUSIONS_SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/JfobsPZnKhCkOkt7ExVl8TRWgGc"

_sheet_id_cache: dict[str, str] = {}


def _spreadsheet_token(sheet_url: str) -> str:
    """Extract the spreadsheet token from a Lark /sheets/ URL. Duplicated
    (not imported) from lark_auth.py's private helper of the same name — this
    repo's own convention (see handle_callback.py's copy) for a one-line
    parsing helper that doesn't warrant a shared private import."""
    parsed = urllib.parse.urlparse(sheet_url)
    parts = [p for p in parsed.path.split("/") if p]
    for marker in ("sheets", "spreadsheets"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return sheet_url.strip()


def _discover_sheet_id(spreadsheet_token: str) -> str:
    if spreadsheet_token in _sheet_id_cache:
        return _sheet_id_cache[spreadsheet_token]
    url = f"https://open.larksuite.com/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"

    def make_request():
        token = get_user_access_token()
        return urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {token}"})

    payload = request_json_with_auth_retry(make_request, context="manager_exclusions:discover_sheet_id")
    sheets = ((payload.get("data") or {}).get("sheets")) or []
    sheet_id = sheets[0].get("sheet_id") if sheets else ""
    if sheet_id:
        _sheet_id_cache[spreadsheet_token] = sheet_id
    return sheet_id


def _exclusions_sheet_id() -> str:
    token = _spreadsheet_token(EXCLUSIONS_SHEET_URL)
    return _discover_sheet_id(token)

# Special key that applies to *every* label UID. Managers listed here are
# permanently skipped from all alert tagging logic regardless of which
# release / label they own.
GLOBAL_UID = "__global__"

# Hardcoded global blocklist (always applied, even if the sheet is empty or
# unreachable). Keep entries lowercase, in `first.last` username form.
HARDCODED_GLOBAL_EXCLUSIONS = ("diego.meleiro", "carla.figlia", "eduardo.praca")


def _norm_uid(label_uid: str) -> str:
    return str(label_uid or "").strip()


def _clean_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.lstrip("@").strip()
    if text.lower().endswith("@bytedance.com"):
        text = text[: -len("@bytedance.com")]
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


# ---------------------------------------------------------------------------
# Sheet-backed storage. Read-all / rewrite-one-row, same "good enough without
# a real transaction" tradeoff the tracker sheets already accept elsewhere in
# this codebase (daily_workflow.py, handle_callback.py) — a rare concurrent
# double-write here just means one of two near-simultaneous exclude/unexclude
# calls needs to be retried, not silent data loss.
# ---------------------------------------------------------------------------

def _read_all_rows() -> List[Tuple[int, str, str]]:
    """Return [(row_number, uid, manager), ...]; row_number is 1-based
    including the header row (row 2 = first data row). Blank rows are
    skipped."""
    if not EXCLUSIONS_SHEET_URL:
        return []
    payload = sheet_values_api("GET", EXCLUSIONS_SHEET_URL, _exclusions_sheet_id(), "A2:B5000")
    values = extract_sheet_values(payload)
    out = []
    for i, row in enumerate(values or []):
        uid = str(row[0] or "").strip() if len(row) > 0 else ""
        manager = str(row[1] or "").strip() if len(row) > 1 else ""
        if uid and manager:
            out.append((i + 2, uid, manager))
    return out


def load_exclusions() -> dict:
    """Return {uid: [manager, ...]}, grouped from the flat sheet."""
    try:
        grouped: dict[str, list[str]] = {}
        for _, uid, manager in _read_all_rows():
            cleaned = _clean_identifier(manager)
            if cleaned and cleaned not in grouped.setdefault(uid, []):
                grouped[uid].append(cleaned)
        for managers in grouped.values():
            managers.sort(key=str.lower)
        return grouped
    except Exception:
        return {}


def save_exclusions(payload: dict) -> None:
    """Full rewrite: clear the sheet's data rows and re-append everything in
    `payload` ({uid: [manager, ...]}). Used rarely (e.g. a bulk import) —
    add_exclusion/remove_exclusion below do a targeted single-row write
    instead for the common case."""
    if not EXCLUSIONS_SHEET_URL:
        return
    sheet_id = _exclusions_sheet_id()
    existing_rows = _read_all_rows()
    last_row = max((r for r, _, _ in existing_rows), default=1)
    rows = []
    for uid, managers in (payload or {}).items():
        for manager in managers or []:
            rows.append([uid, manager])
    # Blank every previously-used data row, then write the new rows from row 2.
    if last_row >= 2:
        blank = [["", ""] for _ in range(last_row - 1)]
        sheet_values_api("PUT", EXCLUSIONS_SHEET_URL, sheet_id, f"A2:B{last_row}", values=blank)
    if rows:
        end_row = 1 + len(rows)
        sheet_values_api("PUT", EXCLUSIONS_SHEET_URL, sheet_id, f"A2:B{end_row}", values=rows)


def ensure_file() -> None:
    """Kept for API compatibility with callers that used to ensure the local
    JSON file existed; a no-op now that storage is a Lark sheet the sheet
    owner creates once, outside the bot."""
    return None


def add_exclusion(label_uid: str, managers: Sequence[str]) -> Tuple[bool, List[str]]:
    uid = _norm_uid(label_uid)
    if not uid or not EXCLUSIONS_SHEET_URL:
        return False, []
    sheet_id = _exclusions_sheet_id()
    existing_rows = _read_all_rows()
    current_for_uid = {m.lower() for _, u, m in existing_rows if u == uid}
    next_row = max((r for r, _, _ in existing_rows), default=1) + 1
    added = []
    for manager in managers:
        cleaned = _clean_identifier(manager)
        if not cleaned or cleaned.lower() in current_for_uid:
            continue
        sheet_values_api("PUT", EXCLUSIONS_SHEET_URL, sheet_id, f"A{next_row}:B{next_row}", values=[[uid, cleaned]])
        current_for_uid.add(cleaned.lower())
        added.append(cleaned)
        next_row += 1
    return True, added


def remove_exclusion(label_uid: str, managers: Sequence[str]) -> Tuple[bool, List[str]]:
    uid = _norm_uid(label_uid)
    if not uid or not EXCLUSIONS_SHEET_URL:
        return False, []
    sheet_id = _exclusions_sheet_id()
    target_aliases = set()
    for manager in managers:
        target_aliases.update(_identifier_aliases(manager))
    removed = []
    for row_number, row_uid, row_manager in _read_all_rows():
        if row_uid != uid:
            continue
        if _identifier_aliases(row_manager) & target_aliases:
            sheet_values_api("PUT", EXCLUSIONS_SHEET_URL, sheet_id, f"A{row_number}:B{row_number}", values=[["", ""]])
            removed.append(row_manager)
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
