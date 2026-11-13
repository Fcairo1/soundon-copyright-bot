#!/usr/bin/env python3
"""Monthly Content Safety report card: what the ACRCloud takedowns protected.

Cumulative over every ACR scan tab. Once a month it:

  1. Reads every scan tab of the ACR results sheet and keeps one entry per
     matched Spotify track (the infringing track), with the most streams any
     scan saw for it, its region and the SoundOn account it infringes.
  2. Asks the Spotify API whether each track is still available in its own
     market (same check as the weekly status check: offline = 404, or not
     playable in its market AND in a second one). Offline verdicts are cached
     in runtime/, so later months only re-check what was still online.
  3. Protected streams = streams of the tracks now offline. Protected revenue
     = those streams x the account's Spotify pay-per-stream (PPS, from
     runtime/content_safety_pps.json; accounts without a known PPS use their
     market's average rate and are labelled estimates).
  4. Growth: weekly Spotify streams (api_sptf_play_cnt_7d, dataset 1576005) of
     OUR original song the week before the takedown vs. up to 2 weeks after.
     The takedown day is unknown for scans, so it is assumed 2 weeks after the
     last scan that still found the infringing track.
  5. Builds the card (layout agreed with the user on 2026-11-13) and sends it
     to the configured recipient: "me" (Filipe's DM) until the user switches
     the config to "group" (the Content Safety + Protection chat).

Self-throttled: run_monthly() does nothing unless the current month has no
report yet, so daily_workflow can call it every day (PART N).
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import acr_digest as ad  # noqa: E402

CONFIG_FILE = Path(__file__).with_name("content_safety_report_config.json")
# Per-account Spotify pay-per-stream rates are royalty data: they live only on the
# bot's machine (runtime/ is git-ignored, this repo is public). Shape:
# {"accounts": {user_id: pps}, "region_fallback": {"BR": .., "US": .., "SPLA": .., "other": ..},
#  "account_names": {user_id: name}, "source": "...", "updated": "YYYY-MM-DD"}
PPS_FILE = ROOT / "runtime" / "content_safety_pps.json"
STATE_FILE = ROOT / "runtime" / "content_safety_report_state.json"
STATUS_CACHE_FILE = ROOT / "runtime" / "content_safety_track_status.json"
RUN_LOCK_HOURS = 6                 # another regional run that started < 6h ago owns this month
TAKEDOWN_LAG_DAYS = 14             # assumed: Spotify removes a track ~2 weeks after the last scan that found it
AFTER_WINDOW_DAYS = 14             # "after" = up to 2 weeks after the takedown
MIN_AFTER_DAYS = 7                 # need at least 1 week of "after" data
MIN_BEFORE_STREAMS = 50_000        # small songs swing too much (new releases ramping up)
TOP_GROWTH = 5
SPOTIFY_PAUSE_SECONDS = 0.2

_TRACK_RE = re.compile(r"track[/:]([A-Za-z0-9]{22})")
_TAB_DATE_RE = re.compile(r"([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2})")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}

REGION_STYLE = {   # tag color + slice shades (biggest account first, "others" last)
    "US": ("blue", ["#3370FF", "#7AA2FF", "#B8CEFF"]),
    "BR": ("green", ["#2EA121", "#6BC95F", "#B3E3AB"]),
    "SPLA": ("orange", ["#FF7D00", "#FFA54D", "#FFCC99"]),
    "other": ("neutral", ["#8F959E", "#BBBFC4", "#DEE0E3"]),
}
REGION_LABEL = {"US": "US", "BR": "Brazil", "SPLA": "SPLA", "other": "Other"}


# ── small helpers ───────────────────────────────────────────────────────────

def load_config(path: Path = CONFIG_FILE) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def _num(raw) -> float:
    s = re.sub(r"[^\d.]", "", str(raw or ""))
    try:
        return float(s)
    except ValueError:
        return 0.0


def _first_artist(raw: str) -> str:
    return str(raw or "").replace("\\u0026", "&").strip('[]"').split('","')[0].split(", ")[0].strip()


def _song_name(raw: str) -> str:
    return re.sub(r"\.(flac|wav|mp3)$", "", str(raw or ""), flags=re.I).strip()


def region_of(country: str) -> str:
    return ad.COUNTRY_TO_REGION.get((country or "").strip().upper(), "other")


def market_of(country: str) -> str:
    c = (country or "").strip().upper()
    return c if re.fullmatch(r"[A-Z]{2}", c) else "US"


def scan_date(tab_name: str, today: date) -> Optional[date]:
    """'Aug3', 'Oct 15', 'Sep22' -> a date in the last 12 months (tab names carry no year)."""
    m = _TAB_DATE_RE.search(tab_name or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    month, day = _MONTHS[m.group(1).lower()], int(m.group(2))
    try:
        d = date(today.year, month, day)
        return d if d <= today else date(today.year - 1, month, day)
    except ValueError:
        return None


def fmt_count(n: float) -> str:
    n = float(n)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{n:.0f}"


def fmt_money(usd: float) -> str:
    return f"${usd / 1e3:.0f}K" if usd >= 100_000 else f"${usd / 1e3:.1f}K"


# ── 1. rows from the scan tabs ──────────────────────────────────────────────

ROW_FIELDS = ("SO Song ID", "SO Song Title", "SO ISRC", "SO User ID", "SO Artist", "SO Region", "Spotify",
              "Streamings", "Matched Title", "Matched ISRC")


def rows_from_tab(name: str, grid: List[List[str]], today: date) -> List[Dict[str, object]]:
    if not grid:
        return []
    idx = ad._header_index(grid[0])
    if not ad.is_valid_cycle_tab(idx):
        return []
    when = scan_date(name, today)
    out = []
    for raw in grid[1:]:
        row = {f: ad._cell(raw, idx.get(f)) for f in ROW_FIELDS}
        if not (row["SO Song Title"] or row["Matched Title"]):
            continue
        row["scan"] = when
        out.append(row)
    return out


def read_scan_rows(today: date) -> Tuple[List[Dict[str, object]], List[date]]:
    rows, scans = [], []
    for tab in ad.list_tabs(ad.ACR_SHEET_URL):
        try:
            grid = ad.read_tab(ad.ACR_SHEET_URL, tab["sheet_id"])
        except Exception as exc:
            print(f"  ⚠ content safety report: could not read tab {tab['name']!r} ({exc!r})", flush=True)
            continue
        tab_rows = rows_from_tab(tab["name"], grid, today)
        if tab_rows:
            rows.extend(tab_rows)
            if tab_rows[0]["scan"]:
                scans.append(tab_rows[0]["scan"])
    return rows, sorted(scans)


# ── 2. one entry per infringing track ───────────────────────────────────────

def build_tracks(rows: Sequence[Dict[str, object]], *, excluded_track_ids: Iterable[str] = (),
                 do_not_claim_keys: Iterable[str] = (), isrc_to_user: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    excluded, dnc = set(excluded_track_ids), set(do_not_claim_keys)
    song2user: Dict[str, str] = {}
    for r in rows:
        if r["SO User ID"]:
            song2user[r["SO Song ID"]] = r["SO User ID"]
    for r in rows:
        if not r["SO User ID"] and r["SO Song ID"] not in song2user and (isrc_to_user or {}).get(r["SO ISRC"]):
            song2user[r["SO Song ID"]] = isrc_to_user[r["SO ISRC"]]

    tracks: Dict[str, dict] = {}
    for r in rows:
        m = _TRACK_RE.search(r["Spotify"])
        if not m or m.group(1) in excluded:
            continue
        if ad.case_key(r["SO Song ID"], r["SO Song Title"], r["Matched ISRC"], r["Matched Title"]) in dnc:
            continue
        tid = m.group(1)
        t = tracks.setdefault(tid, {"streams": 0.0, "countries": collections.Counter(), "regions": collections.Counter(),
                                    "accounts": collections.Counter(), "first": r["scan"], "last": r["scan"],
                                    "title": r["Matched Title"], "originals": {}})
        t["streams"] = max(t["streams"], _num(r["Streamings"]))
        t["countries"][market_of(r["SO Region"])] += 1
        t["regions"][region_of(market_of(r["SO Region"]))] += 1
        t["accounts"][r["SO User ID"] or song2user.get(r["SO Song ID"], "") or "(unknown)"] += 1
        if r["scan"]:
            t["first"] = min(t["first"], r["scan"]) if t["first"] else r["scan"]
            t["last"] = max(t["last"], r["scan"]) if t["last"] else r["scan"]
        song = _song_name(r["SO Song Title"])
        artist = _first_artist(r["SO Artist"])
        t["originals"][(song.lower(), artist.lower())] = {"song": song, "artist": artist, "isrc": r["SO ISRC"],
                                                          "region": region_of(market_of(r["SO Region"]))}
    for t in tracks.values():
        known = [(n, a) for a, n in t["accounts"].items() if a != "(unknown)"]
        t["owner"] = max(known)[1] if known else "(unknown)"
        t["region"] = t["regions"].most_common(1)[0][0]
        t["market"] = t["countries"].most_common(1)[0][0]
    return tracks


# ── 3. availability on Spotify ──────────────────────────────────────────────

def check_availability(tracks: Dict[str, dict], client, cache: Dict[str, dict], *, today: date,
                       sleep=time.sleep) -> Dict[str, int]:
    """Fills tracks[tid]["state"] (offline / online / unknown). Offline verdicts
    are final (cached); everything else is asked again."""
    stats = collections.Counter()
    for tid, t in tracks.items():
        cached = cache.get(tid) or {}
        if cached.get("state") == "offline":
            t["state"] = "offline"
            stats["cached_offline"] += 1
            continue
        verdict = client.check(tid, t["market"])
        sleep(SPOTIFY_PAUSE_SECONDS)
        t["state"] = verdict["state"]
        stats[verdict["state"]] += 1
        cache[tid] = {"state": verdict["state"], "detail": verdict.get("detail", ""), "checked": today.isoformat()}
    return dict(stats)


# ── 4. numbers ──────────────────────────────────────────────────────────────

def summarize(tracks: Dict[str, dict], pps_cfg: Dict[str, object]) -> Dict[str, object]:
    down = {tid: t for tid, t in tracks.items() if t.get("state") == "offline"}
    acc = collections.defaultdict(lambda: {"down": 0, "streams": 0.0, "regions": collections.Counter()})
    for t in tracks.values():
        acc[t["owner"]]["regions"].update(t["regions"])
    for t in down.values():
        a = acc[t["owner"]]
        a["down"] += 1
        a["streams"] += t["streams"]
    known = {str(k): float(v) for k, v in (pps_cfg.get("accounts") or {}).items()}
    fallback = {k: float(v) for k, v in (pps_cfg.get("region_fallback") or {}).items()}
    accounts = []
    for uid, a in acc.items():
        if not a["down"]:
            continue
        region = a["regions"].most_common(1)[0][0] if a["regions"] else "other"
        real = known.get(uid, 0) > 0
        rate = known[uid] if real else fallback.get(region, fallback.get("other", 0.0))
        accounts.append({"uid": uid, "region": region, "down": a["down"], "streams": a["streams"],
                         "rate": rate, "real_rate": real, "revenue": a["streams"] * rate})
    accounts.sort(key=lambda a: -a["revenue"])
    rev_region = collections.Counter()
    for a in accounts:
        rev_region[a["region"]] += a["revenue"]
    return {
        "found": len(tracks), "down": len(down), "streams": sum(t["streams"] for t in down.values()),
        "down_by_region": dict(collections.Counter(t["region"] for t in down.values())),
        "accounts": accounts, "accounts_protected": len(accounts),
        "revenue": sum(a["revenue"] for a in accounts),
        "revenue_real": sum(a["revenue"] for a in accounts if a["real_rate"]),
        "revenue_by_region": dict(rev_region),
        "unknown_status": sum(1 for t in tracks.values() if t.get("state") == "unknown"),
    }


def growth_plan(tracks: Dict[str, dict], latest: date) -> Dict[str, dict]:
    """{isrc: {song, artist, est_td, before_d, after_d, copies}} for originals of taken-down tracks."""
    plan: Dict[str, dict] = {}
    for tid, t in tracks.items():
        if t.get("state") != "offline" or not t["last"]:
            continue
        for o in t["originals"].values():
            if not o["isrc"]:
                continue
            p = plan.setdefault(o["isrc"], {"song": o["song"], "artist": o["artist"], "last": t["last"], "copies": set()})
            p["last"] = max(p["last"], t["last"])
            p["copies"].add(tid)
    out = {}
    for isrc, p in plan.items():
        td = p["last"] + timedelta(days=TAKEDOWN_LAG_DAYS)
        after = min(td + timedelta(days=AFTER_WINDOW_DAYS), latest)
        if after < td + timedelta(days=MIN_AFTER_DAYS):
            continue
        out[isrc] = {**p, "est_td": td, "before_d": td - timedelta(days=1), "after_d": after}
    return out


def growth_rows(plan: Dict[str, dict], streams: Dict[Tuple[str, str], int], top: int = TOP_GROWTH) -> List[Dict[str, object]]:
    """Per song (ISRCs of one song summed): before/after weekly streams; the top
    `top` by % change among songs that grew, ordered by extra streams."""
    songs: Dict[Tuple[str, str], dict] = {}
    for isrc, p in plan.items():
        b = streams.get((isrc, p["before_d"].isoformat()))
        a = streams.get((isrc, p["after_d"].isoformat()))
        if b is None or a is None or b < MIN_BEFORE_STREAMS:
            continue
        s = songs.setdefault((p["song"].lower(), p["artist"].lower()), {"song": p["song"], "artist": p["artist"],
                                                                         "before": 0, "after": 0, "copies": set()})
        s["before"] += b
        s["after"] += a
        s["copies"] |= p["copies"]
    grew = [s for s in songs.values() if s["after"] > s["before"]]
    best = sorted(grew, key=lambda s: -(s["after"] / s["before"]))[:top]
    return sorted(({**s, "copies": len(s["copies"])} for s in best), key=lambda s: -(s["after"] - s["before"]))


# ── 5. Aeolus (dataset 1576005) ─────────────────────────────────────────────

def _aeolus(sql: str) -> List[Dict[str, object]]:
    from copyright_alert import run_alert as ra
    parsed = ra._run_aeolus_sql(sql, timeout=300, dataset_id=ra.ENGAGEMENT_DATASET)
    return ra._aeolus_rows_to_dict(parsed) if parsed else []


def _table() -> str:
    from copyright_alert import run_alert as ra
    return ra.ENGAGEMENT_TABLE


def _quote(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("'", "\\'")


def aeolus_latest_partition(today: date, query=_aeolus) -> Optional[date]:
    since = (today - timedelta(days=21)).isoformat()
    rows = query(f"SELECT MAX(`[p_date]`) AS d FROM `{_table()}` WHERE `[p_date]` >= '{since}'")
    raw = str((rows[0] if rows else {}).get("d") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def aeolus_isrc_owner(isrcs: Sequence[str], p_date: date, query=_aeolus) -> Dict[str, str]:
    out: Dict[str, str] = {}
    isrcs = sorted({i for i in isrcs if i})
    for i in range(0, len(isrcs), 40):
        in_list = ", ".join(f"'{_quote(v)}'" for v in isrcs[i:i + 40])
        for r in query(f"SELECT `[isrc]` AS isrc, `[user_id_str]` AS uid FROM `{_table()}` WHERE `[isrc]` IN ({in_list}) "
                       f"AND `[p_date]` = '{p_date.isoformat()}' GROUP BY `[isrc]`, `[user_id_str]`"):
            if r.get("isrc") and r.get("uid"):
                out.setdefault(str(r["isrc"]), str(r["uid"]))
    return out


def aeolus_account_names(uids: Sequence[str], p_date: date, query=_aeolus) -> Dict[str, str]:
    out: Dict[str, str] = {}
    uids = sorted({u for u in uids if u and u != "(unknown)"})
    for i in range(0, len(uids), 40):
        in_list = ", ".join(f"'{_quote(v)}'" for v in uids[i:i + 40])
        for r in query(f"SELECT `[user_id_str]` AS uid, `[user_name]` AS uname FROM `{_table()}` WHERE `[user_id_str]` IN ({in_list}) "
                       f"AND `[p_date]` = '{p_date.isoformat()}' GROUP BY `[user_id_str]`, `[user_name]`"):
            if r.get("uid") and r.get("uname"):
                out.setdefault(str(r["uid"]), str(r["uname"]))
    return out


def aeolus_weekly_streams(plan: Dict[str, dict], query=_aeolus) -> Dict[Tuple[str, str], int]:
    by_date: Dict[str, set] = collections.defaultdict(set)
    for isrc, p in plan.items():
        by_date[p["before_d"].isoformat()].add(isrc)
        by_date[p["after_d"].isoformat()].add(isrc)
    isrcs = sorted(plan)
    dates = sorted(by_date)
    out: Dict[Tuple[str, str], int] = {}
    for i in range(0, len(isrcs), 40):
        in_list = ", ".join(f"'{_quote(v)}'" for v in isrcs[i:i + 40])
        for j in range(0, len(dates), 8):
            d_list = ", ".join(f"'{d}'" for d in dates[j:j + 8])
            for r in query(f"SELECT `[isrc]` AS isrc, `[p_date]` AS d, SUM(`[api_sptf_play_cnt_7d]`) AS s FROM `{_table()}` "
                           f"WHERE `[isrc]` IN ({in_list}) AND `[p_date]` IN ({d_list}) GROUP BY `[isrc]`, `[p_date]`"):
                key = (str(r.get("isrc") or ""), str(r.get("d") or "")[:10])
                if key[0] and key[0] in by_date.get(key[1], ()):
                    out[key] = int(_num(r.get("s")))
    return out


# ── 6. the card ─────────────────────────────────────────────────────────────

def _md(content: str, **kw) -> dict:
    return {"tag": "markdown", "content": content, **kw}


def _short(name: str, n: int = 26) -> str:
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def donut_slices(accounts: List[dict], names: Dict[str, str], *, max_per_region: int = 2,
                 min_share: float = 0.05) -> List[Tuple[str, float, str]]:
    """[(label, revenue $K, color)]: per region (biggest first) its top accounts
    above `min_share` of the total, then one "N other <region>" slice."""
    total = sum(a["revenue"] for a in accounts) or 1.0
    by_region: Dict[str, List[dict]] = collections.defaultdict(list)
    for a in accounts:
        by_region[a["region"]].append(a)
    order = sorted(by_region, key=lambda r: -sum(a["revenue"] for a in by_region[r]))
    out = []
    for reg in order:
        accs = sorted(by_region[reg], key=lambda a: -a["revenue"])
        shades = REGION_STYLE.get(reg, REGION_STYLE["other"])[1]
        shown = [a for a in accs[:max_per_region] if a["revenue"] / total >= min_share] or accs[:1]
        for i, a in enumerate(shown):
            label = _short(names.get(a["uid"]) or f"Account …{a['uid'][-4:]}", 22)
            out.append((label, round(a["revenue"] / 1e3, 1), shades[min(i, len(shades) - 2)]))
        rest = accs[len(shown):]
        if rest:
            label = f"{len(rest)} other {reg}" if reg != "other" else f"{len(rest)} other"
            out.append((label, round(sum(a["revenue"] for a in rest) / 1e3, 1), shades[-1]))
    return out


def build_card(data: Dict[str, object], growth: List[Dict[str, object]], names: Dict[str, str], *,
               scans: List[date], checked: date, period_label: str, button_url: str,
               previous: Optional[Dict[str, object]] = None) -> dict:
    rev, streams = data["revenue"], data["streams"]
    estimated = data["revenue_real"] < rev
    approx = "~" if estimated else ""
    since = ""
    if previous and previous.get("down") is not None and data["down"] > int(previous["down"]):
        since = f"  <text_tag color='green'>+{data['down'] - int(previous['down'])} since last report</text_tag>"

    hero = {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top", "padding": "14px 12px 14px 12px",
         "vertical_spacing": "0px", "background_style": "blue-50", "elements": [
             _md("<font color='grey'>Streams protected</font>", text_size="notation"),
             _md(f"# <font color='blue-600'>{fmt_count(streams)}</font>")]},
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top", "padding": "14px 12px 14px 12px",
         "vertical_spacing": "0px", "background_style": "wathet-50", "elements": [
             _md("<font color='grey'>Spotify revenue protected</font>", text_size="notation"),
             _md(f"# <font color='wathet-600'>{approx}{fmt_money(rev)}</font>")]}]}
    tags = _md(f"<text_tag color='blue'>{data['down']:,} infringing tracks taken down</text_tag>  "
               f"<font color='grey'>out of {data['found']:,} found · {data['accounts_protected']} accounts protected</font>{since}",
               text_align="center")

    reg_rev = sorted(data["revenue_by_region"].items(), key=lambda kv: -kv[1])
    reg_tags = _md(" ".join(f"<text_tag color='{REGION_STYLE.get(r, REGION_STYLE['other'])[0]}'>{REGION_LABEL.get(r, r)} "
                            f"{fmt_money(v)}</text_tag>" for r, v in reg_rev if v > 0), text_align="center")
    slices = donut_slices(data["accounts"], names)
    donut = {"tag": "chart", "aspect_ratio": "4:3", "chart_spec": {
        "type": "pie", "data": {"values": [{"k": k, "v": v, "l": f"{k}  ${v}K"} for k, v, _ in slices]},
        "valueField": "v", "categoryField": "k", "outerRadius": 0.75, "innerRadius": 0.5, "padAngle": 1.5,
        "pie": {"style": {"cornerRadius": 4}},
        "color": {"type": "ordinal", "domain": [k for k, _, _ in slices], "range": [c for _, _, c in slices]},
        "label": {"visible": True, "position": "outside", "formatter": "{l}", "style": {"fontSize": 10, "lineWidth": 0}},
        "legends": {"visible": False},
        "indicator": {"visible": True, "fixed": True,
                      "title": {"style": {"text": fmt_money(rev), "fontSize": 18, "fontWeight": "bold"}},
                      "content": [{"style": {"text": f"{data['accounts_protected']} accounts", "fontSize": 10}}]}}}

    elements = [hero, tags,
                _md("**Revenue protected, by account** <font color='grey'>(colored by region)</font>"), reg_tags, donut]

    if growth:
        pct = lambda b, a: f"+{(a / b - 1) * 100:.0f}%"
        gain = lambda b, a: f"<font color='green-600'>**+{fmt_count(a - b)}**</font> <font color='grey'>{pct(b, a)}</font>"
        rows = [{"song": _short(g["song"]), "cp": str(g["copies"]), "b": fmt_count(g["before"]),
                 "a": f"**{fmt_count(g['after'])}**", "g": gain(g["before"], g["after"])} for g in growth]
        B, A = sum(g["before"] for g in growth), sum(g["after"] for g in growth)
        rows.append({"song": f"**All {len(growth)} songs**", "cp": f"**{sum(g['copies'] for g in growth)}**",
                     "b": f"**{fmt_count(B)}**", "a": f"**{fmt_count(A)}**", "g": gain(B, A)})
        elements += [
            {"tag": "hr"},
            _md("**After the infringing tracks came down, our songs got more streams**\n"
                "<font color='grey'>Weekly Spotify streams of our original song: the week before the takedown vs. 1–2 weeks after.</font>"),
            {"tag": "table", "page_size": len(rows), "row_height": "low", "freeze_first_column": False,
             "header_style": {"text_align": "left", "text_size": "normal", "background_style": "grey", "bold": True, "lines": 2},
             "columns": [
                 {"name": "song", "display_name": "Our song", "data_type": "markdown", "width": "auto"},
                 {"name": "cp", "display_name": "Infringing tracks removed", "data_type": "markdown", "width": "auto", "horizontal_align": "right"},
                 {"name": "b", "display_name": "Before", "data_type": "markdown", "width": "auto", "horizontal_align": "right"},
                 {"name": "a", "display_name": "After", "data_type": "markdown", "width": "auto", "horizontal_align": "right"},
                 {"name": "g", "display_name": "Extra / week", "data_type": "markdown", "width": "auto", "horizontal_align": "right"}],
             "rows": rows}]

    span = ""
    if scans:
        first, last = scans[0], scans[-1]
        span = f" ({first:%b}–{last:%b %Y})" if (first.year, first.month) != (last.year, last.month) else f" ({last:%b %Y})"
    rate_note = ("Revenue: each account's real Spotify pay-per-stream where we have it; "
                 "otherwise its market's average rate (estimate)." if estimated else
                 "Revenue: each account's real Spotify pay-per-stream.")
    elements.append(_md(f"<font color='grey'>{len(scans)} ACRCloud scans{span}, every track re-checked on Spotify "
                        f"{checked:%b} {checked.day}. {rate_note}</font>", text_size="notation"))
    elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "Open the Content Protection dashboard"},
                     "type": "primary_filled", "width": "fill", "behaviors": [{"type": "open_url", "default_url": button_url}]})

    return {"schema": "2.0",
            "config": {"update_multi": True, "width_mode": "default",
                       "summary": {"content": f"Content Safety: {fmt_count(streams)} streams and {approx}{fmt_money(rev)} Spotify revenue protected"}},
            "header": {"title": {"tag": "plain_text", "content": "Content Safety · ACRCloud results"},
                       "subtitle": {"tag": "plain_text", "content": "Infringing tracks of SoundOn music taken down on Spotify"},
                       "template": "blue", "icon": {"tag": "standard_icon", "token": "done_outlined", "color": "blue"},
                       "text_tag_list": [{"tag": "text_tag", "text": {"tag": "plain_text", "content": period_label}, "color": "blue"}]},
            "body": {"direction": "vertical", "padding": "12px 12px 20px 12px", "vertical_spacing": "12px", "elements": elements}}


# ── 7. send ─────────────────────────────────────────────────────────────────

def send_card(card: dict, cfg: Dict[str, object]) -> Dict[str, object]:
    from copyright_alert import bot_runtime
    to = str(cfg.get("recipient") or "me")
    if to == "group":
        path, receive_id = "/im/v1/messages?receive_id_type=chat_id", str(cfg.get("group_chat_id") or ad.CONTENT_SAFETY_CHAT_ID)
    else:
        path, receive_id = "/im/v1/messages?receive_id_type=email", str(cfg["me_email"])
    resp = bot_runtime._post_api(path, {"receive_id": receive_id, "msg_type": "interactive",
                                        "content": json.dumps(card, ensure_ascii=False)})
    ok = isinstance(resp, dict) and resp.get("code") == 0
    return {"ok": ok, "to": to, "message_id": ((resp or {}).get("data") or {}).get("message_id") if ok else None,
            "error": None if ok else str(resp)[:300]}


# ── run ─────────────────────────────────────────────────────────────────────

def build_report(*, today: date, cfg: Dict[str, object], client, query=_aeolus, sleep=time.sleep,
                 cache: Optional[Dict[str, dict]] = None, rows=None, scans=None, dnc_keys=None,
                 previous: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    if rows is None:
        rows, scans = read_scan_rows(today)
    dnc_keys = ad.read_do_not_claim_keys() if dnc_keys is None else dnc_keys
    latest = aeolus_latest_partition(today, query) or (today - timedelta(days=7))
    missing = [r["SO ISRC"] for r in rows if not r["SO User ID"] and r["SO ISRC"]]
    isrc_owner = aeolus_isrc_owner(missing, latest, query) if missing else {}
    excluded = (cfg.get("excluded_spotify_tracks") or {}).keys()
    tracks = build_tracks(rows, excluded_track_ids=excluded, do_not_claim_keys=dnc_keys, isrc_to_user=isrc_owner)
    cache = _read_json(STATUS_CACHE_FILE, {}) if cache is None else cache
    status = check_availability(tracks, client, cache, today=today, sleep=sleep)
    pps = _read_json(PPS_FILE, {})
    data = summarize(tracks, pps)
    plan = growth_plan(tracks, latest)
    growth = growth_rows(plan, aeolus_weekly_streams(plan, query)) if plan else []
    top_uids = [a["uid"] for a in data["accounts"][:12]]
    names = {**(pps.get("account_names") or {}), **aeolus_account_names(top_uids, latest, query)}
    card = build_card(data, growth, names, scans=scans or [], checked=today, period_label=f"{today:%b %Y}",
                      button_url=str(cfg.get("button_url") or ad.DASHBOARD_URL), previous=previous)
    return {"card": card, "data": data, "growth": growth, "status": status, "cache": cache, "latest_partition": latest}


def _month(today: date) -> str:
    return f"{today:%Y-%m}"


def run_monthly(*, today: Optional[date] = None, force: bool = False, dry_run: bool = False,
                client=None, now: Optional[datetime] = None) -> Dict[str, object]:
    """Once per calendar month (any day from cfg['report_day']). Safe to call
    from every regional daily run: the first one claims the month."""
    cfg = load_config()
    today = today or datetime.now(timezone(timedelta(hours=-3))).date()
    now = now or datetime.now(timezone.utc)
    state = _read_json(STATE_FILE, {})
    month = _month(today)
    if not force:
        if not cfg.get("enabled"):
            return {"skipped": "disabled in content_safety_report_config.json"}
        if today.day < int(cfg.get("report_day") or 1):
            return {"skipped": f"report day is {cfg.get('report_day')}"}
        if state.get("sent_month") == month:
            return {"skipped": f"already sent for {month}"}
        started = state.get("running_since")
        if state.get("running_month") == month and started:
            age_h = (now - datetime.fromisoformat(started)).total_seconds() / 3600
            if age_h < RUN_LOCK_HOURS:
                return {"skipped": f"another run started {age_h:.1f}h ago"}
    if client is None:
        from copyright_alert.spotify_status_check import SpotifyClient
        client = SpotifyClient.from_env()
        if client is None:
            return {"skipped": "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set"}
        client.max_wait = 120
    if not dry_run:
        _write_json(STATE_FILE, {**state, "running_month": month, "running_since": now.isoformat()})
    try:
        report = build_report(today=today, cfg=cfg, client=client, previous=state.get("last"))
    except Exception:
        if not dry_run:
            _write_json(STATE_FILE, {k: v for k, v in state.items() if k not in ("running_month", "running_since")})
        raise
    data = report["data"]
    summary = {"found": data["found"], "down": data["down"], "streams": int(data["streams"]),
               "revenue": round(data["revenue"]), "revenue_real": round(data["revenue_real"]),
               "accounts": data["accounts_protected"], "down_by_region": data["down_by_region"],
               "unknown_status": data["unknown_status"], "spotify": report["status"],
               "growth": [(g["song"], g["before"], g["after"], g["copies"]) for g in report["growth"]],
               "latest_partition": str(report["latest_partition"])}
    if dry_run:
        return {"dry_run": True, "summary": summary, "card": report["card"]}
    _write_json(STATUS_CACHE_FILE, report["cache"])
    sent = send_card(report["card"], cfg)
    new_state = {k: v for k, v in state.items() if k not in ("running_month", "running_since")}
    if sent["ok"]:
        new_state.update({"sent_month": month, "last": {"month": month, **summary}})
    _write_json(STATE_FILE, new_state)
    return {"sent": sent, "summary": summary}


def run_monthly_safe() -> Dict[str, object]:
    try:
        return run_monthly()
    except Exception as exc:
        print(f"  ✗ content safety report failed: {exc!r}", flush=True)
        return {"error": repr(exc)}


def main() -> int:
    p = argparse.ArgumentParser(description="Monthly Content Safety report card")
    p.add_argument("--dry-run", action="store_true", help="compute and print, send nothing, write no state")
    p.add_argument("--force", action="store_true", help="ignore the once-a-month guard")
    p.add_argument("--card-out", help="also write the card JSON here")
    args = p.parse_args()
    result = run_monthly(force=args.force, dry_run=args.dry_run)
    if args.card_out and result.get("card"):
        Path(args.card_out).write_text(json.dumps(result["card"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "card"}, ensure_ascii=False, indent=2, default=str))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
