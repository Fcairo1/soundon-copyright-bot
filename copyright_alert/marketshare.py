#!/usr/bin/env python3
"""Marketshare-per-claimed-UPC — how much of a track's home country's Spotify
plays SoundOn's original recording holds, before and after a claim.

Metric (agreed spec, verified against real data 2026-09-21/22):
    sptf_ms_multi_days = sum(api_sptf_play_cnt_1d) / sum(fixed_stream)
  over a rolling 30-day window, computed in the TRACK'S OWN COUNTRY only (not
  the claim's BR/US/SPLA region bucket — SPLA alone spans 7 countries, and
  marketshare targets are per-country). api_sptf_play_cnt_1d is FUGA-reported
  Spotify plays, NOT "royalty-eligible from the official Spotify API" — do not
  relabel it. Both dataset 1576005 ("[AOP] dm_distribution_song_country_df")
  and a UPC's ISRC list + country (from dataset 374690, "[[AOP] Song
  Dimension]") were confirmed live via bytedcli before writing this file.

Stored per claim (two tracker columns, ppm — parts per million; the dashboard
converts to % for display, ppm/10000 = percent):
  Marketshare At Claim (ppm)   — frozen the first time a claim is seen; the
                                  30d window ends on Date Received. Never
                                  rewritten (see enrich_with_marketshare_once).
  Marketshare Current (ppm)    — refreshed daily, OPEN claims only (see
                                  bucket() below); ending on the latest
                                  available partition.

A UPC's marketshare is the sum of its ISRCs' plays over the sum of the
country's market total for the same window (dedupe ISRCs; do not average).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

from copyright_alert import run_alert as ra

SONG_DIM_DATASET = ra.AEOLUS_DATASET               # "374690"
SONG_DIM_TABLE = "[[AOP] Song Dimension]"
ENGAGEMENT_DATASET = ra.ENGAGEMENT_DATASET          # "1576005"
ENGAGEMENT_TABLE = ra.ENGAGEMENT_TABLE              # "[[AOP] dm_distribution_song_country_df]"

MARKETSHARE_AT_CLAIM_HEADER = "Marketshare At Claim (ppm)"
MARKETSHARE_CURRENT_HEADER = "Marketshare Current (ppm)"

_LOST_STATUS_MARKERS = ("confirm takedown",)
_TERMINAL_STATUS_MARKERS = ("confirm takedown", "resolved", "fraudulent")
_RECENT_LOOKUP_WINDOW_DAYS = 21   # Song Dimension needs a p_date lower bound; matches run_alert's convention
_UPC_CACHE_TTL_SECONDS = 3600

_upc_cache: Dict[str, Dict[str, object]] = {}   # upc -> {"isrcs": [...], "country": str, "fetched_at": float}


def _status_text(value) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum() or ch.isspace()).strip()


def _is_agree_reply(email_status) -> bool:
    return "agree" in str(email_status or "").lower()


def bucket(status, email_status, retracted) -> Optional[str]:
    """Classify one claim into 'lost' / 'protected' / 'at_risk', or None for a
    claim with no status yet (not part of any aggregate)."""
    status_text = _status_text(status)
    if str(retracted or "").strip().lower() == "yes":
        return "protected"
    if not status_text:
        return None
    if any(m in status_text for m in _LOST_STATUS_MARKERS):
        return "lost"
    if "resolved" in status_text:
        return "lost" if _is_agree_reply(email_status) else "protected"
    if "fraudulent" in status_text:
        return "protected"
    return "at_risk"   # blank-but-something / investigating / disputing


def is_open(status, email_status, retracted) -> bool:
    return bucket(status, email_status, retracted) == "at_risk"


def _quote(value) -> str:
    return "'" + ra._aeolus_sql_quote(value) + "'"


def resolve_upc(upc: str) -> Optional[Dict[str, object]]:
    """Return {"isrcs": [...], "country": "BR"} for one UPC, cached. None if
    Aeolus has no data for it. A UPC whose songs span more than one country
    (compilations) uses whichever country has the most recent activity."""
    upc = str(upc or "").strip()
    if not upc or upc.upper() == "N/A":
        return None
    cached = _upc_cache.get(upc)
    if cached and time.time() - cached["fetched_at"] < _UPC_CACHE_TTL_SECONDS:
        return cached
    cutoff = (datetime.utcnow() - timedelta(days=_RECENT_LOOKUP_WINDOW_DAYS)).strftime("%Y-%m-%d")
    sql = (
        f"SELECT `[isrc]`, `[user_region]`, MAX(`[p_date]`) AS latest "
        f"FROM `{SONG_DIM_TABLE}` "
        f"WHERE `[upc]` = {_quote(upc)} AND `[p_date]` >= {_quote(cutoff)} "
        "GROUP BY `[isrc]`, `[user_region]`"
    )
    parsed = ra._run_aeolus_sql(sql, timeout=120, dataset_id=SONG_DIM_DATASET)
    rows = ra._aeolus_rows_to_dict(parsed) if parsed else []
    if not rows:
        return None
    isrcs = sorted({str(r.get("isrc") or "").strip() for r in rows if r.get("isrc")})
    by_country: Dict[str, str] = {}
    for r in rows:
        country = str(r.get("user_region") or "").strip()
        latest = str(r.get("latest") or "")
        if country and latest > by_country.get(country, ""):
            by_country[country] = latest
    country = max(by_country, key=lambda c: by_country[c]) if by_country else ""
    if not isrcs or not country:
        return None
    result = {"isrcs": isrcs, "country": country, "fetched_at": time.time()}
    _upc_cache[upc] = result
    return result


def _marketshare_ppm(isrcs: Sequence[str], country: str, end_date, *, window_days: int = 30) -> Optional[float]:
    """sum(api_sptf_play_cnt_1d) / sum(fixed_stream) for one country over
    [end_date - window_days + 1, end_date], in parts per million."""
    if not isrcs or not country:
        return None
    end = end_date if isinstance(end_date, date) else datetime.strptime(str(end_date)[:10], "%Y-%m-%d").date()
    start = end - timedelta(days=window_days - 1)
    start_s, end_s = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    in_list = ", ".join(_quote(v) for v in isrcs)

    num_sql = (
        f"SELECT SUM(`[api_sptf_play_cnt_1d]`) AS n FROM `{ENGAGEMENT_TABLE}` "
        f"WHERE `[isrc]` IN ({in_list}) AND `[country_code]` = {_quote(country)} "
        f"AND `[p_date]` >= {_quote(start_s)} AND `[p_date]` <= {_quote(end_s)}"
    )
    den_sql = (
        "SELECT SUM(m) AS d FROM (SELECT `[country_code]`, `[p_date]`, "
        f"MAX(`[valid_total_approx_sptf_play_cnt_1d]`) AS m FROM `{ENGAGEMENT_TABLE}` "
        f"WHERE `[country_code]` = {_quote(country)} "
        f"AND `[p_date]` >= {_quote(start_s)} AND `[p_date]` <= {_quote(end_s)} "
        "GROUP BY `[country_code]`, `[p_date]`)"
    )
    num_parsed = ra._run_aeolus_sql(num_sql, timeout=180, dataset_id=ENGAGEMENT_DATASET)
    den_parsed = ra._run_aeolus_sql(den_sql, timeout=180, dataset_id=ENGAGEMENT_DATASET)
    num_rows = ra._aeolus_rows_to_dict(num_parsed) if num_parsed else []
    den_rows = ra._aeolus_rows_to_dict(den_parsed) if den_parsed else []
    try:
        num = float(num_rows[0].get("n") or 0) if num_rows else 0.0
        den = float(den_rows[0].get("d") or 0) if den_rows else 0.0
    except (TypeError, ValueError, IndexError):
        return None
    if den <= 0:
        return None
    return round(num / den * 1_000_000, 3)


def get_marketshare_ppm_for_upc(upc: str, end_date) -> Optional[float]:
    resolved = resolve_upc(upc)
    if not resolved:
        return None
    return _marketshare_ppm(resolved["isrcs"], resolved["country"], end_date)


def get_marketshare_ppm_for_upcs(upcs: Sequence[str], end_date) -> Dict[str, Optional[float]]:
    """Batch version, grouped so each distinct (country, isrc-set) pair is
    queried once even when several claims share a UPC."""
    out: Dict[str, Optional[float]] = {}
    groups: Dict[tuple, List[str]] = {}
    for upc in dict.fromkeys(str(u or "").strip() for u in upcs if str(u or "").strip()):
        resolved = resolve_upc(upc)
        if not resolved:
            out[upc] = None
            continue
        key = (resolved["country"], tuple(resolved["isrcs"]))
        groups.setdefault(key, []).append(upc)
    for (country, isrcs), upcs_in_group in groups.items():
        ppm = _marketshare_ppm(list(isrcs), country, end_date)
        for upc in upcs_in_group:
            out[upc] = ppm
    return out
