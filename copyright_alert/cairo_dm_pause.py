#!/usr/bin/env python3
"""Helpers for temporarily pausing Cairo-specific private DMs in BRT."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAUSE_TZ = ZoneInfo("America/Sao_Paulo")
PAUSE_START = date(2026, 11, 14)
PAUSE_END = date(2026, 11, 30)
CAIRO_EMAIL = "filipe.cairo@bytedance.com"
P2P_CHAT_CACHE_FILE = ROOT / "copyright_alert" / "bot_p2p_chats.json"
_CAIRO_OPEN_ID_CACHE = ""
_CAIRO_CHAT_IDS_CACHE: tuple[str, ...] = ()


def _resolve_cairo_open_id() -> str:
    global _CAIRO_OPEN_ID_CACHE
    if _CAIRO_OPEN_ID_CACHE:
        return _CAIRO_OPEN_ID_CACHE
    try:
        from copyright_alert.dm_action_card import resolve_open_id

        resolved = (resolve_open_id(CAIRO_EMAIL) or "").strip()
    except Exception:
        resolved = ""
    if resolved:
        _CAIRO_OPEN_ID_CACHE = resolved
    return resolved


def _cached_cairo_chat_ids() -> tuple[str, ...]:
    global _CAIRO_CHAT_IDS_CACHE
    if _CAIRO_CHAT_IDS_CACHE:
        return _CAIRO_CHAT_IDS_CACHE
    cairo_open_id = _resolve_cairo_open_id()
    try:
        payload = json.loads(P2P_CHAT_CACHE_FILE.read_text(encoding="utf-8")) if P2P_CHAT_CACHE_FILE.exists() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return ()

    chat_ids: set[str] = set()
    by_open_id = payload.get("by_open_id") or {}
    if cairo_open_id and isinstance(by_open_id, dict):
        record = by_open_id.get(cairo_open_id) or {}
        if isinstance(record, dict):
            chat_id = str(record.get("chat_id") or "").strip()
            if chat_id:
                chat_ids.add(chat_id)

    by_chat_id = payload.get("by_chat_id") or {}
    if cairo_open_id and isinstance(by_chat_id, dict):
        for chat_id, record in by_chat_id.items():
            if not isinstance(record, dict):
                continue
            if str(record.get("operator_open_id") or "").strip() == cairo_open_id:
                cid = str(chat_id or record.get("chat_id") or "").strip()
                if cid:
                    chat_ids.add(cid)

    if chat_ids:
        _CAIRO_CHAT_IDS_CACHE = tuple(sorted(chat_ids))
        return _CAIRO_CHAT_IDS_CACHE
    return ()


def is_cairo_dm_paused(*, recipient_email: str = "", recipient_open_id: str = "", recipient_chat_id: str = "") -> bool:
    today_brt = datetime.now(PAUSE_TZ).date()
    if today_brt < PAUSE_START or today_brt > PAUSE_END:
        return False

    email = str(recipient_email or "").strip().lower()
    if email and email == CAIRO_EMAIL.lower():
        return True

    open_id = str(recipient_open_id or "").strip()
    cairo_open_id = _resolve_cairo_open_id()
    if open_id and cairo_open_id and open_id == cairo_open_id:
        return True

    chat_id = str(recipient_chat_id or "").strip()
    if chat_id and chat_id in _cached_cairo_chat_ids():
        return True

    return False
