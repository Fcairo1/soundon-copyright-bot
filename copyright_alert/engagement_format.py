"""Formatting helpers for the Spotify engagement numbers shown on claim cards
and manager reminders.

Pure functions, no I/O — shared by run_alert (group cards), tag_managers
(reminder cards) and engagement_tracker (daily refresh).

Importance tiers are intentionally simple thresholds on the 30-day Spotify
stream count. Override with env vars if ops wants a different cut-off.
"""

from __future__ import annotations

import os
from typing import Optional

HIGH_STREAMS = int(os.getenv("STREAMS_HIGH_THRESHOLD", "100000"))
MID_STREAMS = int(os.getenv("STREAMS_MID_THRESHOLD", "10000"))

_EMPTY = {"", "N/A", "NA", "NULL", "NONE", "-", "—"}


def parse_count(value) -> Optional[float]:
    """Parse an engagement cell/Aeolus value ("12345", "12,345", 1.2e5, "N/A")
    into a non-negative float, or None when it isn't a usable number."""
    text = str(value if value is not None else "").strip().replace(",", "")
    if text.upper() in _EMPTY:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number < 0:  # NaN / negative
        return None
    return number


def format_compact(value) -> str:
    """1234567 -> "1.2M", 340000 -> "340K", 4200 -> "4.2K", 850 -> "850",
    None -> "—"."""
    n = parse_count(value)
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        k = n / 1_000
        if round(k) >= 1000:
            return "1M"
        text = f"{k:.0f}" if k >= 100 else f"{k:.1f}".rstrip("0").rstrip(".")
        return text + "K"
    return str(int(round(n)))


def is_high(value) -> bool:
    """True for tracks at/above the high-streaming threshold (cards get restyled)."""
    n = parse_count(value)
    return n is not None and n >= HIGH_STREAMS


def tier_badge(value) -> str:
    """🔥 for high-streaming tracks, ⭐ for mid, "" otherwise."""
    n = parse_count(value)
    if n is None:
        return ""
    if n >= HIGH_STREAMS:
        return "🔥"
    if n >= MID_STREAMS:
        return "⭐"
    return ""


def trend_text(now, baseline) -> str:
    """"↑ 8% since claim" / "↓ 12% since claim" / "" (flat or not comparable)."""
    n, b = parse_count(now), parse_count(baseline)
    if n is None or b is None:
        return ""
    if b == 0:
        return "↑ new" if n > 0 else ""
    pct = (n - b) / b * 100
    if abs(pct) < 1:
        return ""
    return f"{'↑' if pct > 0 else '↓'} {abs(pct):.0f}% since claim"


def streams_info(now=None, baseline=None, refreshed: bool = False) -> dict:
    """Normalized streams payload used by the card/reminder renderers.

    ``now`` is the latest daily-refreshed figure (falls back to the at-claim
    snapshot when the refresh hasn't run yet); ``baseline`` is the at-claim
    snapshot. The trend is only shown once a real refresh exists.
    """
    now_n, base_n = parse_count(now), parse_count(baseline)
    current = now_n if now_n is not None else base_n
    return {
        "now": current,
        "baseline": base_n,
        "refreshed": bool(refreshed and now_n is not None),
    }


def streams_line(info: Optional[dict]) -> str:
    """Card body line: "🔥 **1.2M** · ↑ 8% since claim"."""
    info = info or {}
    current = info.get("now")
    if current is None:
        return "—"
    parts = [f"{tier_badge(current)} **{format_compact(current)}**".strip()]
    if info.get("refreshed"):
        trend = trend_text(current, info.get("baseline"))
        if trend:
            parts.append(trend)
    return " · ".join(parts)


def streams_suffix(info: Optional[dict]) -> str:
    """Reminder-line suffix: " — 🎧 1.2M 🔥" (always present so managers can
    tell "unknown" from "low")."""
    info = info or {}
    current = info.get("now")
    badge = tier_badge(current)
    return f" — 🎧 {format_compact(current)}" + (f" {badge}" if badge else "")
